"""PostgreSQL races for AI request admission; fixture owns random test databases."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from hashlib import sha256
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from project_copilot.db.models import AiRequestReservation, UserAccount
from project_copilot.services.quotas import IdempotencyConflict, QuotaExceeded, QuotaService, QuotaStateError


def create_users(db, count):
    ids = [uuid4() for _ in range(count)]
    with db.transaction() as session:
        session.add_all([UserAccount(id=user_id, username=f"test{i:02}", password_hash="$argon2id$fake") for i, user_id in enumerate(ids)])
    return ids


def fingerprint(value):
    return sha256(value.encode()).hexdigest()


def test_idempotency_and_unknown_are_fail_closed(ticket_database):
    db = ticket_database
    user, other = create_users(db, 2)
    with db.transaction() as session:
        row, reused = QuotaService(session).reserve(user, "request-123", fingerprint("one"), "agent")
        assert not reused and row.state == "reserved" and row.cost_status == "unknown"
        row_id = row.id
    with db.transaction() as session:
        row, reused = QuotaService(session).reserve(user, "request-123", fingerprint("one"), "agent")
        assert reused and row.id == row_id
    with pytest.raises(IdempotencyConflict):
        with db.transaction() as session:
            QuotaService(session).reserve(user, "request-123", fingerprint("changed"), "agent")
    with pytest.raises(IdempotencyConflict):
        with db.transaction() as session:
            QuotaService(session).reserve(user, "request-123", fingerprint("one"), "rag")
    with db.transaction() as session:
        service = QuotaService(session)
        assert service.claim(user, row_id)
        assert service.mark_provider_attempt(user, row_id) == 1
        service.finish(user, row_id, outcome="unknown", usage={"prompt_tokens": 42})
    with pytest.raises(QuotaExceeded, match="quota") as error:
        with db.transaction() as session:
            QuotaService(session).reserve(user, "request-456", fingerprint("two"), "agent")
    assert error.value.reason == "user_concurrency"
    with pytest.raises(QuotaStateError):
        with db.transaction() as session:
            QuotaService(session).finish(user, row_id, outcome="succeeded")
    with db.transaction() as session:
        row = session.get(AiRequestReservation, row_id)
        assert (row.state, row.finished_at, row.provider_attempts, row.usage, row.cost_status) == ("unknown", None, 1, {"prompt_tokens": 42}, "unknown")
        assert QuotaService(session).reserve(other, "request-123", fingerprint("one"), "agent")[1] is False
    assert db.engine.pool.checkedout() == 0


def test_daily_counts_utc_and_terminal_failures_are_counted(ticket_database):
    db = ticket_database
    user = create_users(db, 1)[0]
    for n, state in enumerate(("succeeded", "failed")):
        with db.transaction() as session:
            service = QuotaService(session, user_daily_limit=2)
            row, _ = service.reserve(user, f"request-{n:03}", fingerprint(str(n)), "rag")
            assert row.utc_day == session.scalar(select(func.clock_timestamp())).astimezone(timezone.utc).date()
            assert service.claim(user, row.id)
            service.finish(user, row.id, outcome=state, usage={"total_tokens": n + 1, "reasoning_tokens": n})
    with pytest.raises(QuotaExceeded) as error:
        with db.transaction() as session:
            QuotaService(session, user_daily_limit=2).reserve(user, "request-999", fingerprint("x"), "agent")
    assert error.value.reason == "user_daily"
    # Move a completed synthetic record into the preceding day to exercise the
    # date boundary without changing the database or machine clock.
    with db.transaction() as session:
        row = session.scalar(select(AiRequestReservation).where(AiRequestReservation.idempotency_key == "request-000"))
        row.utc_day -= timedelta(days=1)
    with db.transaction() as session:
        assert not QuotaService(session, user_daily_limit=2).reserve(user, "request-999", fingerprint("x"), "agent")[1]
    assert db.engine.pool.checkedout() == 0


def test_same_user_concurrent_requests_only_one_is_admitted(ticket_database):
    db = ticket_database
    user = create_users(db, 1)[0]
    barrier = Barrier(6)

    def run(i):
        barrier.wait(timeout=10)
        try:
            with db.transaction() as session:
                row, _ = QuotaService(session).reserve(user, f"request-{i:03}", fingerprint(str(i)), "agent")
                return row.id
        except QuotaExceeded as exc:
            assert exc.reason == "user_concurrency"
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, range(6)))
    assert sum(item is not None for item in results) == 1
    assert db.engine.pool.checkedout() == 0


def test_global_daily_limit_is_atomic_across_users(ticket_database):
    db = ticket_database
    users = create_users(db, 8)
    barrier = Barrier(len(users))

    def run(user):
        barrier.wait(timeout=10)
        try:
            with db.transaction() as session:
                QuotaService(session, global_daily_limit=3).reserve(user, "shared-key", fingerprint("x"), "agent")
            return "admitted"
        except QuotaExceeded as exc:
            return exc.reason

    with ThreadPoolExecutor(max_workers=len(users)) as pool:
        result = list(pool.map(run, users))
    assert result.count("admitted") == 3 and result.count("global_daily") == 5
    assert db.engine.pool.checkedout() == 0


def test_claim_once_and_rollback_reservation(ticket_database):
    db = ticket_database
    user = create_users(db, 1)[0]
    with pytest.raises(RuntimeError):
        with db.transaction() as session:
            QuotaService(session).reserve(user, "rolled-back", fingerprint("x"), "agent")
            raise RuntimeError("job insert failed")
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 0
        row, _ = QuotaService(session).reserve(user, "claim-key", fingerprint("x"), "agent")
        row_id = row.id
    barrier = Barrier(4)

    def claim(_):
        barrier.wait(timeout=10)
        with db.transaction() as session:
            return QuotaService(session).claim(user, row_id)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(claim, range(4))) == [False, False, False, True]
    with db.transaction() as session:
        row = session.get(AiRequestReservation, row_id)
        assert row.state == "running" and row.provider_attempts == 0
    assert db.engine.pool.checkedout() == 0


def test_provider_attempt_budget_is_atomic(ticket_database):
    db = ticket_database
    user = create_users(db, 1)[0]
    with db.transaction() as session:
        quota = QuotaService(session)
        row, _ = quota.reserve(user, "attempt-key", fingerprint("x"), "upload")
        job_id = row.id
        assert quota.claim(user, job_id)
    barrier = Barrier(5)

    def attempt(_):
        barrier.wait(timeout=10)
        try:
            with db.transaction() as session:
                QuotaService(session).mark_provider_attempt(user, job_id, max_attempts=2)
            return "allowed"
        except QuotaExceeded as exc:
            return exc.reason

    with ThreadPoolExecutor(max_workers=5) as pool:
        result = list(pool.map(attempt, range(5)))
    assert result.count("allowed") == 2 and result.count("provider_attempts") == 3
    with db.transaction() as session:
        assert session.get(AiRequestReservation, job_id).provider_attempts == 2
    for invalid in (0, True, -1):
        with pytest.raises(ValueError):
            with db.transaction() as session:
                QuotaService(session).mark_provider_attempt(user, job_id, max_attempts=invalid)
    assert db.engine.pool.checkedout() == 0
