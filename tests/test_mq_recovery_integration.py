"""New opt-in jobs only: real PG dispatch/recovery, synthetic execution boundaries."""
from time import sleep
from uuid import uuid4
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from project_copilot.db.jobs import AiJob
from project_copilot.db.messaging import AiJobOutbox
from project_copilot.db.models import AiRequestReservation
from project_copilot.db.recovery import AiJobExecutionAttempt, AiJobRecoveryState
from project_copilot.infrastructure.execution_tracking import provider_request
from project_copilot.services import release_control as release
from project_copilot.services.job_recovery import initialize_recovery, recover_pending
from project_copilot.services.messaging import enqueue_outbox, JobMessage
from project_copilot.services.mq_worker import process_message
from project_copilot.services.quotas import QuotaService
from scripts.week1_queue_lab import synthetic_result
from test_personal_demo_jobs import setup_personal


def enrolled(database, monkeypatch, tmp_path, *, timeout=900):
    settings, user, _, login, grant = setup_personal(database, monkeypatch)
    settings.message_queue_enabled = True
    settings.rabbitmq_url = SecretStr("amqp://synthetic:unused@localhost:15672/test")
    settings.job_recovery_enabled = True
    settings.manifest_dir = tmp_path / "manifests"
    settings.personal_agent_total_timeout_seconds = timeout
    with database.transaction() as session:
        from project_copilot.db.release import ReleaseControl
        control = session.get(ReleaseControl, 1)
        from project_copilot.services.personal_profile import personal_limits
        control.evidence = {**control.evidence, "configuration_fingerprint": release.config_fingerprint(settings),
                            "limits": personal_limits(settings)}
        session.flush()
        row, reused = QuotaService(session, settings=settings).reserve(user, "mq-recovery-" + uuid4().hex, "a" * 64, "rag")
        assert not reused
        session.add(AiJob(id=row.id, login_session_id=login, payload={"question": "synthetic recovery"}))
        event = enqueue_outbox(session, row.id)
        assert initialize_recovery(session, row.id, settings) is not None
    return settings, event


def latest(database, job_id):
    with database.transaction() as session:
        row = session.scalar(select(AiJobOutbox).where(AiJobOutbox.job_id == job_id)
                             .order_by(AiJobOutbox.dispatch_generation.desc()).limit(1))
        return JobMessage(row.event_id, row.job_id, row.dispatch_generation)


def test_proven_ended_zero_provider_failure_requeues_once_and_keeps_one_reservation(ticket_database, monkeypatch, tmp_path):
    database = ticket_database
    settings, event = enrolled(database, monkeypatch, tmp_path)
    attempts = []
    def first(_settings, _database, job):
        attempts.append(job.attempt_no)
        raise RuntimeError("synthetic local error")
    assert process_message(database, settings, event, runner=first) == "deferred"
    with database.transaction() as session:
        row, state = session.get(AiRequestReservation, event.job_id), session.get(AiJobRecoveryState, event.job_id)
        assert row.state == "reserved" and row.slot_released_at is None and row.provider_attempts == 0
        assert state.current_attempt_no == 2
        assert session.get(AiJobExecutionAttempt, (event.job_id, 1)).previous_state == "unknown"
        deadline = state.deadline_at
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 1
    replacement = latest(database, event.job_id)
    assert replacement.dispatch_generation == 2
    def second(_settings, _database, job):
        attempts.append(job.attempt_no)
        assert job.deadline_at == deadline
        return synthetic_result(job)
    assert process_message(database, settings, replacement, runner=second) == "completed"
    assert process_message(database, settings, event, runner=first) == "invalid"
    assert process_message(database, settings, replacement, runner=first) == "completed"
    assert attempts == [1, 2]
    with database.transaction() as session:
        row = session.get(AiRequestReservation, event.job_id)
        assert row.state == "succeeded" and row.provider_attempts == 0 and row.slot_released_at is not None


def test_unknown_second_attempt_is_retained_and_never_replayed(ticket_database, monkeypatch, tmp_path):
    database = ticket_database
    settings, event = enrolled(database, monkeypatch, tmp_path)
    calls = []
    def fail(_settings, _database, job):
        calls.append(job.attempt_no)
        raise RuntimeError("synthetic local error")
    assert process_message(database, settings, event, runner=fail) == "deferred"
    second = latest(database, event.job_id)
    assert process_message(database, settings, second, runner=fail) == "completed"
    recover_pending(database, settings)
    assert process_message(database, settings, second, runner=fail) == "completed"
    assert calls == [1, 2]
    with database.transaction() as session:
        assert session.get(AiRequestReservation, event.job_id).state == "unknown"
        assert session.get(AiJobRecoveryState, event.job_id).blocked_reason == "attempt_limit"
        assert session.scalar(select(func.count()).select_from(AiJobOutbox)) == 2


def test_provider_uncertainty_blocks_automatic_recovery_even_with_repeat_policy(ticket_database, monkeypatch, tmp_path):
    database = ticket_database
    settings, event = enrolled(database, monkeypatch, tmp_path)
    # This records a synthetic provider boundary only; no HTTP request occurs.
    def uncertain(*_):
        with provider_request():
            raise TimeoutError("synthetic unobserved provider outcome")
    assert process_message(database, settings, event, runner=uncertain) == "completed"
    decisions = recover_pending(database, settings)
    assert any(item.reason == "provider_uncertain" for item in decisions)
    with database.transaction() as session:
        row = session.get(AiRequestReservation, event.job_id)
        assert row.state == "unknown" and row.provider_attempts == 1 and row.slot_released_at is None
        assert session.get(AiJobRecoveryState, event.job_id).current_attempt_no == 1
        assert session.scalar(select(func.count()).select_from(AiJobOutbox)) == 1


def test_original_deadline_stops_provider_initiation_without_refund(ticket_database, monkeypatch, tmp_path):
    database = ticket_database
    settings, event = enrolled(database, monkeypatch, tmp_path, timeout=.02)
    def expired(*_):
        sleep(.04)
        with provider_request():
            pytest.fail("expired provider request was permitted")
    assert process_message(database, settings, event, runner=expired) == "completed"
    with database.transaction() as session:
        row = session.get(AiRequestReservation, event.job_id)
        assert row.state == "failed" and row.provider_attempts == 0
        assert session.get(AiJob, event.job_id).failure_type == "total_timeout"
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 1
