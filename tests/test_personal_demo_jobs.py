"""Personal-window quota/worker contracts on isolated PostgreSQL; no providers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, update

from project_copilot.config import Settings
from project_copilot.db.jobs import AiJob
from project_copilot.db.models import AiRequestReservation
from project_copilot.db.release import ReleaseControl
from project_copilot.infrastructure.call_accounting import account_job, record_provider_attempt
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.services import ai_worker, release_control as release
from project_copilot.services.auth import AuthService
from project_copilot.services.entry_protection import source_fingerprint
from project_copilot.services.quotas import QuotaExceeded, QuotaService
from project_copilot.services.personal_profile import personal_limits


def setup_personal(database, monkeypatch):
    settings = Settings(_env_file=None, environment="production", public_demo=True,
                        public_proxy_secret="a" * 40, auth_public_origin="https://personal.example",
                        database_url="postgresql+psycopg://project_copilot_public:synthetic@postgres:5432/project_copilot_public",
                        max_concurrent_requests_per_user=5)
    with database.transaction() as session:
        auth = AuthService(session)
        alice = auth.register(RegisterInput(username="alice", password="synthetic-personal-password"), open_registration=True)
        other = auth.register(RegisterInput(username="other", password="synthetic-personal-password"), open_registration=True)
        issued = auth.login(LoginInput(username="alice", password="synthetic-personal-password"))
        _, login = auth.authenticate(issued.token)
        grant = uuid4()
        now = session.scalar(select(func.clock_timestamp()))
        row = session.get(ReleaseControl, 1)
        row.state, row.worker_enabled = "PERSONAL_DEMO", True
        row.resource_id, row.origin = settings.public_resource_id, str(settings.auth_public_origin).rstrip("/")
        row.code_fingerprint, row.expires_at = release.code_fingerprint(), now + timedelta(minutes=60)
        row.allowed_user_ids = [str(alice.id)]
        # Synthetic authorization fixture, never emitted as deployment evidence.
        row.evidence = {"profile": "personal-demo-v2", "grant_id": str(grant), "started_at": now.isoformat(),
                        "configuration_fingerprint": release.config_fingerprint(settings),
                        "source_sha256": source_fingerprint(), "limits": personal_limits(settings)}
    monkeypatch.setattr(release, "dependencies_ready", lambda *_: None)
    monkeypatch.setattr(ai_worker, "dependencies_ready", lambda *_: None)
    return settings, alice.id, other.id, login.id, grant


def reserve(database, settings, user_id, key):
    with database.transaction() as session:
        row, reused = QuotaService(session, user_concurrency_limit=5, settings=settings).reserve(
            user_id, key, "a" * 64, "rag")
        return row.id, reused


def complete(database, user_id, job_id, *, outcome="failed"):
    with database.transaction() as session:
        quota = QuotaService(session)
        assert quota.claim(user_id, job_id)
        quota.finish(user_id, job_id, outcome=outcome)


def seed_released_history(database, user_id, grant, count):
    """Synthetic ledger history only: no Worker/provider execution or quota refund."""
    with database.transaction() as session:
        now = session.scalar(select(func.clock_timestamp()))
        session.execute(insert(AiRequestReservation), [
            {"user_id": user_id, "personal_demo_grant_id": grant, "idempotency_key": f"synthetic-released-{n:04}",
             "payload_fingerprint": "a" * 64, "kind": "rag", "utc_day": now.astimezone(timezone.utc).date(),
             "state": "failed", "finished_at": now, "slot_released_at": now}
            for n in range(count)])


def test_five_hundred_tasks_survive_failures_replay_and_utc_day_change(ticket_database, monkeypatch):
    database = ticket_database
    settings, user_id, _, _, grant = setup_personal(database, monkeypatch)
    first, _ = reserve(database, settings, user_id, "personal-task-000")
    complete(database, user_id, first)
    assert reserve(database, settings, user_id, "personal-task-000") == (first, True)
    for n in (1, 2):
        job_id, _ = reserve(database, settings, user_id, f"personal-task-{n:03}")
        complete(database, user_id, job_id)
    seed_released_history(database, user_id, grant, 497)
    # UTC daily accounting can reset; this window's lifetime accounting cannot.
    with database.transaction() as session:
        session.execute(update(AiRequestReservation).values(utc_day=AiRequestReservation.utc_day - 1))
    with pytest.raises(QuotaExceeded) as error:
        reserve(database, settings, user_id, "personal-task-003")
    assert error.value.reason == "personal_demo_limit"
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiRequestReservation).where(
            AiRequestReservation.personal_demo_grant_id == grant)) == 500


def test_personal_concurrency_is_three_independent_of_generic_settings(ticket_database, monkeypatch):
    database = ticket_database
    settings, user_id, _, _, _ = setup_personal(database, monkeypatch)
    reserve(database, settings, user_id, "personal-task-000")
    reserve(database, settings, user_id, "personal-task-001")
    reserve(database, settings, user_id, "personal-task-002")
    with pytest.raises(QuotaExceeded) as error:
        reserve(database, settings, user_id, "personal-task-003")
    assert error.value.reason == "user_concurrency"


def test_personal_last_slot_is_atomic_between_requests(ticket_database, monkeypatch):
    database = ticket_database
    settings, user_id, _, _, grant = setup_personal(database, monkeypatch)
    for n in range(2):
        job_id, _ = reserve(database, settings, user_id, f"personal-task-{n:03}")
        complete(database, user_id, job_id)
    seed_released_history(database, user_id, grant, 497)
    barrier = Barrier(4)
    def submit(n):
        barrier.wait(timeout=10)
        try:
            reserve(database, settings, user_id, f"racing-task-{n:03}")
            return "accepted"
        except QuotaExceeded as error:
            return error.reason
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert results.count("accepted") == 1
    assert set(results) <= {"accepted", "personal_demo_limit", "user_concurrency"}


def test_expired_window_cannot_reserve_or_claim_a_queued_task(ticket_database, monkeypatch):
    database = ticket_database
    settings, user_id, _, login_id, _ = setup_personal(database, monkeypatch)
    job_id, _ = reserve(database, settings, user_id, "personal-task-000")
    with database.transaction() as session:
        session.add(AiJob(id=job_id, login_session_id=login_id, payload={"question": "synthetic"}))
        session.get(ReleaseControl, 1).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(release.ReleaseClosed):
        reserve(database, settings, user_id, "personal-task-001")
    assert ai_worker.process_one(database, settings, runner=lambda *_: pytest.fail("expired task executed")) is False
    with database.transaction() as session:
        assert session.get(AiRequestReservation, job_id).state == "reserved"


def test_old_grant_queue_does_not_execute_in_a_new_window(ticket_database, monkeypatch):
    database = ticket_database
    settings, user_id, other, login_id, grant = setup_personal(database, monkeypatch)
    old_grant = uuid4()
    with database.transaction() as session:
        row, _ = QuotaService(session).reserve(other, "old-window-task", "b" * 64, "rag")
        row.personal_demo_grant_id = old_grant
        session.add(AiJob(id=row.id, login_session_id=login_id, payload={"question": "old"}))
        old_id = row.id
    current, _ = reserve(database, settings, user_id, "personal-task-000")
    with database.transaction() as session:
        session.add(AiJob(id=current, login_session_id=login_id, payload={"question": "current"}))
    assert ai_worker.claim_one(database) is None  # no settings cannot bypass a personal grant
    assert ai_worker.claim_one(database, settings=settings).id == current
    with database.transaction() as session:
        assert session.get(AiRequestReservation, old_id).state == "reserved"
        assert session.get(AiRequestReservation, current).personal_demo_grant_id == grant


def test_each_provider_boundary_rechecks_expiry_and_retains_attempt_count(ticket_database, monkeypatch):
    database = ticket_database
    settings, user_id, _, login_id, _ = setup_personal(database, monkeypatch)
    job_id, _ = reserve(database, settings, user_id, "personal-task-000")
    with database.transaction() as session:
        session.add(AiJob(id=job_id, login_session_id=login_id, payload={"question": "synthetic"}))
    assert ai_worker.claim_one(database, settings=settings).id == job_id
    with account_job(database, user_id, job_id, settings=settings):
        record_provider_attempt()  # intercepted boundary only, never a network call
        with database.transaction() as session:
            session.get(ReleaseControl, 1).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        with pytest.raises(release.ReleaseClosed):
            record_provider_attempt()
    with database.transaction() as session:
        row = session.get(AiRequestReservation, job_id)
        assert row.provider_attempts == 1 and row.state == "running"


def test_bound_task_cannot_resume_after_grant_changes(ticket_database, monkeypatch):
    database = ticket_database
    settings, user_id, _, login_id, _ = setup_personal(database, monkeypatch)
    job_id, _ = reserve(database, settings, user_id, "personal-task-000")
    with database.transaction() as session:
        session.add(AiJob(id=job_id, login_session_id=login_id, payload={"question": "synthetic"}))
    assert ai_worker.claim_one(database, settings=settings).id == job_id
    with database.transaction() as session:
        row = session.get(ReleaseControl, 1)
        row.evidence = {**row.evidence, "grant_id": str(uuid4())}
    with account_job(database, user_id, job_id, settings=settings), pytest.raises(release.ReleaseClosed):
        record_provider_attempt()
    with database.transaction() as session:
        assert session.get(AiRequestReservation, job_id).provider_attempts == 0
