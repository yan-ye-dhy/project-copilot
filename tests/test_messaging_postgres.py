"""Owned PostgreSQL dispatch races; brokers, providers and deployment stay controlled."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import func, select, text

from project_copilot.db.jobs import AiJob
from project_copilot.db.messaging import AiJobInbox, AiJobOutbox
from project_copilot.db.migrations import check_model_drift, migration_config, upgrade
from project_copilot.db.models import AiRequestReservation
from project_copilot.services.messaging import (JobMessage, begin_delivery, claim_outbox, complete_delivery,
    defer_delivery, enqueue_outbox, mark_outbox_sent, publish_claim, relay_batch, relay_once)
from project_copilot.services.quotas import QuotaService
from scripts.week1_queue_lab import _settings, enqueue, seed_identity


@pytest.fixture
def dispatch(ticket_database):
    database = ticket_database
    user_id, login_id = seed_identity(database)
    settings = _settings()
    settings.queue_max_pending_jobs = 100

    def create(session=None):
        def insert_in(transaction):
            reservation, reused = QuotaService(transaction, settings=settings, user_daily_limit=100,
                global_daily_limit=100, user_concurrency_limit=100).reserve(
                    user_id, "dispatch-" + uuid4().hex, "f" * 64, "rag")
            assert not reused
            transaction.add(AiJob(id=reservation.id, login_session_id=login_id,
                                  payload={"question": "private synthetic request"}))
            return enqueue_outbox(transaction, reservation.id)
        if session is not None:
            return insert_in(session)
        with database.transaction() as transaction:
            return insert_in(transaction)
    return SimpleNamespace(database=database, user_id=user_id, create=create)


def outcome(dispatch, message, state):
    with dispatch.database.transaction() as session:
        quota = QuotaService(session)
        assert quota.claim(dispatch.user_id, message.job_id)
        if state != "running":
            quota.finish(dispatch.user_id, message.job_id, outcome=state)
            session.get(AiJob, message.job_id).result = {"answer": "durable"} if state == "succeeded" else None


def make_due(database, message, *, inbox=False):
    with database.transaction() as session:
        now = session.scalar(select(func.clock_timestamp()))
        row = session.get(AiJobOutbox, message.event_id)
        row.available_at = now - timedelta(seconds=1)
        row.lease_until = now - timedelta(seconds=1) if row.lease_token else None
        if inbox:
            receipt = session.get(AiJobInbox, message.event_id)
            receipt.available_at = now - timedelta(seconds=1)
            receipt.lease_until = now - timedelta(seconds=1) if receipt.lease_token else None


def test_messaging_migration_orm_and_due_index_match(ticket_database):
    check_model_drift(ticket_database)
    with ticket_database.transaction() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(migration_config()).get_current_head()
        definition = session.scalar(text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_job_outbox_due'"))
        assert "available_at, event_id" in definition and "sent_at IS NULL" in definition


def test_upgrade_preserves_legacy_queue_without_implicit_broker_backfill(postgres_database):
    with postgres_database.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0016_queue_backpressure")
    settings = _settings()
    identities = seed_identity(postgres_database)
    identity, _, _ = enqueue(postgres_database, settings, *identities)
    upgrade(postgres_database)
    with postgres_database.transaction() as session:
        assert session.get(AiRequestReservation, identity).state == "reserved"
        assert session.get(AiJob, identity).payload["question"] == "synthetic queue experiment"
        assert session.scalar(select(func.count()).select_from(AiJobOutbox)) == 0


def test_enqueue_and_rollback_cover_job_reservation_and_event(dispatch):
    with pytest.raises(RuntimeError, match="controlled rollback"):
        with dispatch.database.transaction() as session:
            dispatch.create(session)
            raise RuntimeError("controlled rollback")
    with dispatch.database.transaction() as session:
        for model in (AiRequestReservation, AiJob, AiJobOutbox):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    message = dispatch.create()
    with dispatch.database.transaction() as session:
        assert enqueue_outbox(session, message.job_id) == message
        assert session.scalar(select(func.count()).select_from(AiJobOutbox)) == 1


def test_concurrent_relays_claim_distinct_events_with_short_transactions(dispatch):
    messages = [dispatch.create() for _ in range(4)]
    barrier = Barrier(4)
    def claim(_):
        barrier.wait(timeout=10)
        return claim_outbox(dispatch.database)
    with ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(claim, range(4)))
    assert {claim.message for claim in claims} == set(messages)
    assert len({claim.token for claim in claims}) == 4
    assert claim_outbox(dispatch.database) is None
    assert dispatch.database.engine.pool.checkedout() == 0


def test_publish_occurs_without_pg_connection_and_confirm_marks_sent(dispatch):
    message = dispatch.create()
    published = []
    class Publisher:
        def publish(self, received):
            assert dispatch.database.engine.pool.checkedout() == 0
            with dispatch.database.transaction() as session:
                row = session.get(AiJobOutbox, received.event_id, with_for_update={"nowait": True})
                assert row.sent_at is None and row.publish_attempts == 1
            published.append(received)
    assert relay_batch(dispatch.database, Publisher(), limit=10) == 1
    assert published == [message]
    with dispatch.database.transaction() as session:
        row = session.get(AiJobOutbox, message.event_id)
        assert row.sent_at is not None and row.lease_token is None
    assert not relay_once(dispatch.database, Publisher())


def test_publish_failure_backoff_is_bounded_sanitized_and_not_a_hot_loop(dispatch, caplog):
    message = dispatch.create()
    class Publisher:
        def publish(self, _):
            raise RuntimeError("amqp://private-user:private-password@host/private-prompt")
    for expected_attempt, delay in enumerate((2, 4, 5, 5), start=1):
        assert relay_batch(dispatch.database, Publisher(), retry_base_seconds=2, retry_max_seconds=5) == 1
        with dispatch.database.transaction() as session:
            now = session.scalar(select(func.clock_timestamp()))
            row = session.get(AiJobOutbox, message.event_id)
            assert row.publish_attempts == expected_attempt and row.failure_code == "publish_failed"
            assert row.sent_at is None and row.lease_token is None
            assert delay - 1 < (row.available_at - now).total_seconds() <= delay
        assert claim_outbox(dispatch.database) is None
        make_due(dispatch.database, message)
    assert "private-password" not in caplog.text and "private-prompt" not in caplog.text


def test_expired_relay_owner_cannot_mark_another_owners_confirmation(dispatch):
    message = dispatch.create()
    old = claim_outbox(dispatch.database)
    make_due(dispatch.database, message)
    new = claim_outbox(dispatch.database)
    assert old.token != new.token and old.message == new.message
    assert not mark_outbox_sent(dispatch.database, old)
    assert mark_outbox_sent(dispatch.database, new)


def test_broker_confirm_followed_by_relay_crash_resends_same_event(dispatch):
    message = dispatch.create()
    published = []
    class InterruptedPublisher:
        def publish(self, received):
            published.append(received)
            raise SystemExit(73)  # Simulates process death after broker accepted.
    with pytest.raises(SystemExit):
        relay_once(dispatch.database, InterruptedPublisher())
    assert claim_outbox(dispatch.database) is None
    make_due(dispatch.database, message)
    class Publisher:
        def publish(self, received):
            published.append(received)
    assert relay_once(dispatch.database, Publisher())
    assert published == [message, message]


def test_consumer_deferral_fences_a_publish_confirm_in_flight(dispatch):
    message = dispatch.create()
    relay = claim_outbox(dispatch.database)
    class Publisher:
        def publish(self, received):
            receipt = begin_delivery(dispatch.database, received)
            assert receipt.decision == "ready"
            assert defer_delivery(dispatch.database, receipt, delay_seconds=20)
    assert not publish_claim(dispatch.database, Publisher(), relay)
    assert claim_outbox(dispatch.database) is None
    with dispatch.database.transaction() as session:
        row = session.get(AiJobOutbox, message.event_id)
        receipt = session.get(AiJobInbox, message.event_id)
        assert row.sent_at is None and row.available_at == receipt.available_at
        assert receipt.state == "deferred" and receipt.defer_reason == "capacity"


@pytest.mark.parametrize("state", ["succeeded", "failed", "unknown"])
def test_inbox_completion_requires_durable_worker_outcome_and_is_idempotent(dispatch, state):
    message = dispatch.create()
    receipt = begin_delivery(dispatch.database, message)
    assert receipt.decision == "ready"
    assert not complete_delivery(dispatch.database, receipt)
    outcome(dispatch, message, state)
    assert complete_delivery(dispatch.database, receipt)
    assert complete_delivery(dispatch.database, receipt)
    duplicate = begin_delivery(dispatch.database, message)
    assert duplicate.decision == "completed" and duplicate.token is None
    with dispatch.database.transaction() as session:
        row = session.get(AiJobInbox, message.event_id)
        assert row.state == "completed" and row.outcome == state and row.deliveries == 2
        assert session.get(AiJobOutbox, message.event_id).sent_at is not None
        assert session.get(AiRequestReservation, message.job_id).provider_attempts == 0


def test_rolled_back_result_never_completes_inbox(dispatch):
    message = dispatch.create()
    receipt = begin_delivery(dispatch.database, message)
    outcome(dispatch, message, "running")
    with pytest.raises(RuntimeError):
        with dispatch.database.transaction() as session:
            QuotaService(session).finish(dispatch.user_id, message.job_id, outcome="succeeded")
            session.get(AiJob, message.job_id).result = {"answer": "rolled back"}
            raise RuntimeError("controlled result commit failure")
    assert not complete_delivery(dispatch.database, receipt)
    duplicate = begin_delivery(dispatch.database, message)
    assert duplicate.decision == "deferred"
    with dispatch.database.transaction() as session:
        assert session.get(AiJobInbox, message.event_id).state == "processing"
        assert session.get(AiRequestReservation, message.job_id).state == "running"
        assert session.get(AiJob, message.job_id).result is None


def test_concurrent_duplicate_defers_durably_without_revoking_active_owner(dispatch):
    message = dispatch.create()
    barrier = Barrier(2)
    def deliver(_):
        barrier.wait(timeout=10)
        return begin_delivery(dispatch.database, message, lease_seconds=60)
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(deliver, range(2)))
    assert sorted(receipt.decision for receipt in receipts) == ["deferred", "ready"]
    ready = next(receipt for receipt in receipts if receipt.decision == "ready")
    assert claim_outbox(dispatch.database) is None
    with dispatch.database.transaction() as session:
        row = session.get(AiJobInbox, message.event_id)
        assert row.lease_token == ready.token and row.available_at >= row.lease_until
        assert session.get(AiJobOutbox, message.event_id).available_at == row.available_at
    outcome(dispatch, message, "succeeded")
    assert complete_delivery(dispatch.database, ready)


def test_expired_inbox_can_recover_reserved_job_but_never_running_execution(dispatch):
    message = dispatch.create()
    old = begin_delivery(dispatch.database, message)
    make_due(dispatch.database, message, inbox=True)
    replacement = begin_delivery(dispatch.database, message)
    assert replacement.decision == "ready" and replacement.token != old.token
    outcome(dispatch, message, "running")
    make_due(dispatch.database, message, inbox=True)
    assert begin_delivery(dispatch.database, message).decision == "deferred"
    assert not defer_delivery(dispatch.database, old)
    assert not complete_delivery(dispatch.database, replacement)
    with dispatch.database.transaction() as session:
        row = session.get(AiRequestReservation, message.job_id)
        assert row.state == "running" and row.provider_attempts == 0 and row.slot_released_at is None
        QuotaService(session).finish(dispatch.user_id, message.job_id, outcome="succeeded")
        session.get(AiJob, message.job_id).result = {"answer": "late durable completion"}
    assert not complete_delivery(dispatch.database, old)
    assert complete_delivery(dispatch.database, replacement)


def test_early_duplicate_preserves_defer_deadline_and_unknown_event_has_no_side_effect(dispatch):
    message = dispatch.create()
    receipt = begin_delivery(dispatch.database, message)
    assert defer_delivery(dispatch.database, receipt, delay_seconds=120, reason="release")
    with dispatch.database.transaction() as session:
        deadline = session.get(AiJobInbox, message.event_id).available_at
    assert begin_delivery(dispatch.database, message).decision == "deferred"
    with dispatch.database.transaction() as session:
        assert session.get(AiJobInbox, message.event_id).available_at == deadline
    forged = JobMessage(uuid4(), message.job_id)
    assert begin_delivery(dispatch.database, forged).decision == "invalid"
    with dispatch.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiJobInbox)) == 1


def test_stale_dispatch_generation_cannot_claim_a_reserved_job(dispatch):
    message = dispatch.create()
    with dispatch.database.transaction() as session:
        current = enqueue_outbox(session, message.job_id, dispatch_generation=2)
    assert begin_delivery(dispatch.database, message).decision == "invalid"
    assert begin_delivery(dispatch.database, current).decision == "ready"
