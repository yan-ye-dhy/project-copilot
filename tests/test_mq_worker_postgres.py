"""Real owned PostgreSQL dispatch/execution tests; providers and health are controlled.

These tests exercise the broker callback's return boundary, not an actual
RabbitMQ ACK or a live release. No model or broker endpoint is contacted.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from scripts.week1_queue_lab import seed_identity
from project_copilot.config import Settings
from project_copilot.db.jobs import AiJob, AiJobEvent
from project_copilot.db.messaging import AiJobInbox, AiJobOutbox
from project_copilot.db.models import AiRequestReservation
from project_copilot.infrastructure.call_accounting import record_provider_attempt
from project_copilot.services import ai_worker, release_control
from project_copilot.services.messaging import begin_delivery, enqueue_outbox
from project_copilot.services.mq_worker import process_message
from project_copilot.services.quotas import QuotaService


@pytest.fixture
def mq_jobs(ticket_database, monkeypatch):
    database = ticket_database
    settings = Settings(_env_file=None, environment="test", public_demo=False,
        database_url=database.engine.url.render_as_string(hide_password=False),
        message_queue_enabled=True, rabbitmq_url="amqp://synthetic:synthetic@127.0.0.1:5672/%2F",
        deepseek_api_key=None, zhipu_api_key=None, tracing_enabled=False,
        queue_max_pending_jobs=10, worker_max_active_jobs=4)
    # Only deployment admission/health are substituted. Auth/session, durable
    # quota, job claim, event, inbox and outbox SQL all use the owned database.
    monkeypatch.setattr(ai_worker, "require_open", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_worker, "dependencies_ready", lambda *_args: None)
    monkeypatch.setattr(release_control, "require_open", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(release_control, "dependencies_ready", lambda *_args: None)
    user_id, login_id = seed_identity(database)

    def create():
        with database.transaction() as session:
            reservation, _ = QuotaService(session, user_daily_limit=100,
                global_daily_limit=100, user_concurrency_limit=100).reserve(
                    user_id, "mq-worker-" + uuid4().hex, "a" * 64, "rag")
            session.add(AiJob(id=reservation.id, login_session_id=login_id,
                              payload={"question": "受控消息队列问题"}))
            return enqueue_outbox(session, reservation.id)

    return SimpleNamespace(database=database, settings=settings, user_id=user_id, create=create)


def _result(job):
    return SimpleNamespace(model_dump=lambda **_: {"answer": "受控答案", "job_id": str(job.id)},
                           generation=None)


def test_delivery_executes_its_job_instead_of_an_older_queued_job(mq_jobs):
    setup = mq_jobs
    older, target = setup.create(), setup.create()
    calls = []

    def runner(_settings, _database, job):
        calls.append(job.id)
        return _result(job)

    assert process_message(setup.database, setup.settings, target, runner=runner) == "completed"
    assert calls == [target.job_id]
    with setup.database.transaction() as session:
        assert session.get(AiRequestReservation, older.job_id).state == "reserved"
        assert session.get(AiRequestReservation, target.job_id).state == "succeeded"
        assert session.get(AiJobInbox, target.event_id).outcome == "succeeded"
        assert session.get(AiJobInbox, older.event_id) is None


def test_concurrent_consumers_defer_duplicates_without_executing_twice(mq_jobs):
    setup = mq_jobs
    message = setup.create()
    entered, finish = Event(), Event()
    calls = []

    def runner(_settings, _database, job):
        calls.append(job.id)
        entered.set()
        assert finish.wait(timeout=15), "controlled runner was not released"
        return _result(job)

    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(process_message, setup.database, setup.settings, message, runner=runner)
        try:
            assert entered.wait(timeout=10)
            duplicates = [pool.submit(process_message, setup.database, setup.settings, message,
                                      runner=runner) for _ in range(3)]
            assert [future.result(timeout=10) for future in duplicates] == ["deferred"] * 3
            with setup.database.transaction() as session:
                inbox = session.get(AiJobInbox, message.event_id)
                outbox = session.get(AiJobOutbox, message.event_id)
                assert inbox.state == "processing" and inbox.lease_token is not None
                assert inbox.defer_reason == "active" and inbox.deliveries == 4
                assert outbox.sent_at is None and outbox.available_at >= inbox.lease_until
                assert session.get(AiRequestReservation, message.job_id).state == "running"
        finally:
            finish.set()
        assert first.result(timeout=10) == "completed"
    assert calls == [message.job_id]
    with setup.database.transaction() as session:
        assert session.get(AiJobInbox, message.event_id).state == "completed"
        assert session.get(AiJobOutbox, message.event_id).sent_at is not None
        assert session.scalar(select(func.count()).select_from(AiJobEvent).where(
            AiJobEvent.job_id == message.job_id, AiJobEvent.kind == "terminal")) == 1


def test_lost_ack_redelivery_returns_original_result_without_another_provider_attempt(mq_jobs):
    setup = mq_jobs
    message = setup.create()
    calls = []

    def runner(_settings, _database, job):
        calls.append(job.id)
        assert record_provider_attempt() is True  # accounted substitute; no HTTP
        return _result(job)

    # Simulate losing the ACK after the worker committed its original result.
    assert process_message(setup.database, setup.settings, message, runner=runner) == "completed"
    with setup.database.transaction() as session:
        original = dict(session.get(AiJob, message.job_id).result)
    assert process_message(setup.database, setup.settings, message, runner=runner) == "completed"
    assert calls == [message.job_id]
    with setup.database.transaction() as session:
        reservation = session.get(AiRequestReservation, message.job_id)
        assert reservation.provider_attempts == 1 and reservation.state == "succeeded"
        assert reservation.cost_status == "unknown"
        assert session.get(AiJob, message.job_id).result == original
        assert session.get(AiJobInbox, message.event_id).deliveries == 2
        assert session.scalar(select(func.count()).select_from(AiJobEvent).where(
            AiJobEvent.job_id == message.job_id, AiJobEvent.kind == "terminal")) == 1


@pytest.mark.parametrize("state", ["running", "unknown"])
def test_expired_delivery_lease_never_replays_running_or_unknown_execution(mq_jobs, state):
    setup = mq_jobs
    message = setup.create()
    claim = begin_delivery(setup.database, message)
    assert claim.decision == "ready"
    with setup.database.transaction() as session:
        reservation = session.get(AiRequestReservation, message.job_id)
        reservation.state, reservation.provider_attempts = state, 1
        inbox = session.get(AiJobInbox, message.event_id)
        inbox.lease_until = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
    calls = []
    decision = process_message(setup.database, setup.settings, message,
        runner=lambda *_: calls.append(True))
    assert decision == ("deferred" if state == "running" else "completed")
    assert calls == []
    with setup.database.transaction() as session:
        reservation = session.get(AiRequestReservation, message.job_id)
        inbox = session.get(AiJobInbox, message.event_id)
        outbox = session.get(AiJobOutbox, message.event_id)
        assert reservation.state == state and reservation.provider_attempts == 1
        assert reservation.slot_released_at is None
        if state == "running":
            # Delivery lease expiry does not establish physical execution death.
            # Preserve its owner so a still-running original consumer can finish.
            assert inbox.state == "processing" and inbox.lease_token == claim.token
            assert inbox.defer_reason == "active"
            assert outbox.sent_at is None
            assert outbox.available_at > session.scalar(select(func.clock_timestamp()))
        else:
            assert inbox.state == "completed" and inbox.outcome == "unknown"


def test_closed_worker_commits_deferral_without_executing_or_consuming_quota(mq_jobs, monkeypatch):
    setup = mq_jobs
    message = setup.create()

    def closed(*_args, **_kwargs):
        raise release_control.ReleaseClosed("controlled closed release")

    monkeypatch.setattr(ai_worker, "require_open", closed)
    calls = []
    assert process_message(setup.database, setup.settings, message,
        runner=lambda *_: calls.append(True)) == "deferred"
    assert calls == []
    with setup.database.transaction() as session:
        reservation = session.get(AiRequestReservation, message.job_id)
        inbox = session.get(AiJobInbox, message.event_id)
        outbox = session.get(AiJobOutbox, message.event_id)
        assert reservation.state == "reserved" and reservation.provider_attempts == 0
        assert inbox.state == "deferred" and inbox.lease_token is None
        assert outbox.sent_at is None
        assert outbox.available_at > session.scalar(select(func.clock_timestamp()))


def test_full_execution_capacity_is_a_durable_defer_not_a_dropped_message(mq_jobs):
    setup = mq_jobs
    message = setup.create()
    active = setup.create()
    settings = setup.settings.model_copy(update={"worker_max_active_jobs": 1})
    with setup.database.transaction() as session:
        session.get(AiRequestReservation, active.job_id).state = "running"
    calls = []
    assert process_message(setup.database, settings, message,
        runner=lambda *_: calls.append(True)) == "deferred"
    assert calls == []
    with setup.database.transaction() as session:
        assert session.get(AiRequestReservation, message.job_id).state == "reserved"
        assert session.get(AiJobInbox, message.event_id).defer_reason == "capacity"
        assert session.get(AiJobOutbox, message.event_id).sent_at is None


def test_result_commit_failure_defers_inspection_without_replaying_paid_work(mq_jobs, monkeypatch):
    setup = mq_jobs
    message = setup.create()
    calls = []
    original_append = ai_worker.append_event

    def fail_terminal(session, job_id, kind, data):
        if kind == "terminal":
            raise SQLAlchemyError("synthetic terminal transaction failure")
        return original_append(session, job_id, kind, data)

    def runner(_settings, _database, job):
        calls.append(job.id)
        record_provider_attempt()
        return _result(job)

    monkeypatch.setattr(ai_worker, "append_event", fail_terminal)
    with pytest.raises(SQLAlchemyError, match="synthetic terminal transaction failure"):
        process_message(setup.database, setup.settings, message, runner=runner)
    with setup.database.transaction() as session:
        reservation = session.get(AiRequestReservation, message.job_id)
        assert reservation.state == "running" and reservation.provider_attempts == 1
        assert reservation.slot_released_at is None
        assert session.get(AiJob, message.job_id).result is None
        inbox = session.get(AiJobInbox, message.event_id)
        assert inbox.state == "deferred" and inbox.defer_reason == "active"
        assert session.get(AiJobOutbox, message.event_id).sent_at is None
        # Making the dispatch due cannot authorize a second execution.
        inbox.available_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
    monkeypatch.setattr(ai_worker, "append_event", original_append)
    assert process_message(setup.database, setup.settings, message, runner=runner) == "deferred"
    assert calls == [message.job_id]
    with setup.database.transaction() as session:
        assert session.get(AiRequestReservation, message.job_id).provider_attempts == 1

