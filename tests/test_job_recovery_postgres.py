"""New-job recovery against owned PostgreSQL and real OS leases; no provider calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from project_copilot.agent.proposals import ProposalBackend
from project_copilot.config import Settings
from project_copilot.db.executions import AiJobExecution
from project_copilot.db.jobs import AiJob
from project_copilot.db.messaging import AiJobOutbox
from project_copilot.db.migrations import check_model_drift
from project_copilot.db.models import AiRequestReservation, LoginSession, PendingAction, Ticket
from project_copilot.db.operations import JobRecoveryAudit
from project_copilot.db.recovery import AiJobExecutionAttempt, AiJobRecoveryState, JobBusinessOperation
from project_copilot.infrastructure.execution_tracking import ExecutionTracker, track_execution
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.services import ai_worker, release_control as release
from project_copilot.services.approvals import ApprovalService
from project_copilot.services.auth import AuthService
from project_copilot.services.execution_slots import (
    ExecutionFenced, ExecutionLease, finish_personal_execution, guard_execution,
    mark_local_done_and_release, provider_started,
)
from project_copilot.services.job_recovery import (
    RecoveryBudgetExceeded, charge_recovery_step, initialize_recovery, recover_job, recover_pending,
    recovery_budget,
)
from project_copilot.services.messaging import JobMessage, enqueue_outbox
from project_copilot.services.quotas import QuotaService

PASSWORD = "synthetic-recovery-password-123"
USAGE = {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}


@pytest.fixture
def recovery_setup(ticket_database, tmp_path, monkeypatch):
    database = ticket_database
    settings = Settings(_env_file=None, public_demo=True, environment="production",
        message_queue_enabled=True, job_recovery_enabled=True,
        rabbitmq_url="amqp://synthetic:synthetic@127.0.0.1:5672/%2F",
        public_proxy_secret="synthetic-recovery-secret-" * 3, auth_public_origin="https://personal.example",
        database_url="postgresql+psycopg://project_copilot_public:synthetic@postgres:5432/project_copilot_public",
        manifest_dir=tmp_path / "manifests", tracing_enabled=False)
    # Admission/session/grant SQL is real; no synthetic values certify a live deployment.
    monkeypatch.setattr(release, "code_fingerprint", lambda: "synthetic-recovery-code")
    monkeypatch.setattr(release, "config_fingerprint", lambda _: "a" * 64)
    monkeypatch.setattr(release, "dependencies_ready", lambda *_: None)
    monkeypatch.setattr(ai_worker, "dependencies_ready", lambda *_: None)
    from project_copilot.services import entry_protection
    monkeypatch.setattr(entry_protection, "source_fingerprint", lambda: {"synthetic": "a" * 64})
    with database.transaction() as session:
        auth = AuthService(session)
        admin = auth.bootstrap_admin(RegisterInput(username="admin", password=PASSWORD))
        user = auth.register(RegisterInput(username="alice", password=PASSWORD), open_registration=True)
        issued = auth.login(LoginInput(username="alice", password=PASSWORD))
        _, login = auth.authenticate(issued.token)
        admin_id, user_id, login_id = admin.id, user.id, login.id
        release.change_state(session, settings, actor_id=admin_id, operation="PERSONAL_DEMO",
            personal_user_id=user_id, duration_minutes=90, reason="synthetic recovery test grant")
        release.change_state(session, settings, actor_id=admin_id, operation="worker-enable",
            reason="synthetic recovery worker enable")
    leases = []

    def reserve(kind="rag", *, legacy=False, context_id=None):
        with database.transaction() as session:
            row, _ = QuotaService(session, settings=settings).reserve(
                user_id, "recovery-test-" + uuid4().hex, "f" * 64, kind)
            session.add(AiJob(id=row.id, login_session_id=login_id, context_id=context_id,
                              payload={"question": "controlled recovery"}))
            session.flush()
            enqueue_outbox(session, row.id)
            if not legacy:
                assert initialize_recovery(session, row.id, settings) is not None
            return row.id

    def claim(identity):
        job = ai_worker.claim_one(database, settings=settings, job_id=identity)
        assert job is not None and job.execution_lease is not None
        leases.append(job.execution_lease)
        return job

    yield SimpleNamespace(database=database, settings=settings, admin_id=admin_id, user_id=user_id,
        login_id=login_id, token=issued.token, reserve=reserve, claim=claim, leases=leases)
    for lease in leases:
        lease.close()


def test_migration_matches_models_and_preserves_fractional_original_timeout(recovery_setup):
    setup = recovery_setup
    check_model_drift(setup.database)
    identity = setup.reserve()
    with setup.database.transaction() as session:
        session.get(AiJobRecoveryState, identity).timeout_seconds = 0.25
    job = ai_worker.claim_one(setup.database, settings=setup.settings, job_id=identity)
    setup.leases.append(job.execution_lease)
    with setup.database.transaction() as session:
        state = session.get(AiJobRecoveryState, identity)
        assert (state.deadline_at - state.budget_started_at).total_seconds() == 0.25


def test_safe_dead_execution_archives_then_rotates_without_refunding_job_or_budget(recovery_setup):
    setup = recovery_setup
    identity = setup.reserve()
    first = setup.claim(identity)
    with setup.database.transaction() as session:
        assert charge_recovery_step(session, identity, first.execution_lease.token, kind="model") == 1
        assert charge_recovery_step(session, identity, first.execution_lease.token, kind="tool") == 1
        reservation = session.get(AiRequestReservation, identity)
        reservation.usage = USAGE
        initial = (reservation.idempotency_key, reservation.utc_day, reservation.personal_demo_grant_id)
        deadline = session.get(AiJobRecoveryState, identity).deadline_at
    first.execution_lease.close()
    result = recover_job(setup.database, setup.settings, identity)
    assert (result.decision, result.reason, result.attempt_no) == ("requeued", "no_provider", 2)
    with setup.database.transaction() as session:
        snapshot = session.get(AiJobExecutionAttempt, (identity, 1))
        assert snapshot.token == first.execution_lease.token
        assert snapshot.previous_state == "running" and snapshot.reason == "no_provider"
        assert snapshot.provider_open == 0 and not snapshot.provider_uncertain
        assert snapshot.usage == USAGE and snapshot.model_calls == snapshot.tool_calls == 1
        current = session.get(AiJobExecution, identity)
        assert current.token == snapshot.token and current.fenced_at is not None
        assert session.get(AiRequestReservation, identity).state == "reserved"
    with pytest.raises(ExecutionFenced), setup.database.transaction() as session:
        guard_execution(session, identity, first.execution_lease.token)
    second = setup.claim(identity)
    assert second.execution_lease.token != first.execution_lease.token
    with setup.database.transaction() as session:
        reservation = session.get(AiRequestReservation, identity)
        state = session.get(AiJobRecoveryState, identity)
        assert state.current_attempt_no == 2 and state.deadline_at == deadline
        assert state.model_calls == state.tool_calls == 1
        assert reservation.usage == USAGE and reservation.provider_attempts == 0
        assert (reservation.idempotency_key, reservation.utc_day, reservation.personal_demo_grant_id) == initial
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 1
        assert session.scalar(select(func.count()).select_from(AiJobOutbox)) == 2
        assert guard_execution(session, identity, second.execution_lease.token).token == second.execution_lease.token
    with pytest.raises(ExecutionFenced), setup.database.transaction() as session:
        provider_started(session, identity, first.execution_lease.token)
    obsolete = ExecutionLease.try_acquire(setup.settings.manifest_dir, identity,
        first.execution_lease.token, first.execution_lease.inode)
    assert obsolete is not None
    try:
        with pytest.raises(ExecutionFenced), setup.database.transaction() as session:
            finish_personal_execution(session, first, obsolete, outcome="succeeded", usage=USAGE)
        with pytest.raises(ExecutionFenced):
            mark_local_done_and_release(setup.database, setup.settings, identity, obsolete.token, obsolete)
    finally:
        obsolete.close()
    second.execution_lease.close()
    assert recover_job(setup.database, setup.settings, identity).reason == "attempt_limit"


@pytest.mark.parametrize("problem", ["live", "missing", "corrupt", "provider_open", "provider_uncertain"])
def test_unsafe_physical_or_provider_evidence_is_never_requeued(recovery_setup, problem):
    setup = recovery_setup
    identity = setup.reserve()
    job = setup.claim(identity)
    lease = job.execution_lease
    if problem != "live":
        lease.close()
    if problem == "missing":
        lease.path.unlink()  # Only this test's freshly owned, private synthetic proof.
    elif problem == "corrupt":
        lease.path.write_text("synthetic corrupted proof")
    elif problem in {"provider_open", "provider_uncertain"}:
        with setup.database.transaction() as session:
            proof = session.get(AiJobExecution, identity)
            if problem == "provider_open":
                proof.provider_open = 1
            else:
                proof.provider_uncertain = True
    result = recover_job(setup.database, setup.settings, identity, allow_model_repeat=True,
                         checkpoint_available=True)
    assert result.decision == "blocked"
    expected = problem if problem.startswith("provider_") else "live_or_invalid_proof"
    assert result.reason == expected
    with setup.database.transaction() as session:
        assert session.get(AiRequestReservation, identity).state == "running"
        assert session.get(AiRequestReservation, identity).slot_released_at is None
        assert session.get(AiJobRecoveryState, identity).current_attempt_no == 1
        assert session.get(AiJobRecoveryState, identity).blocked_reason == expected
        assert session.scalar(select(func.count()).select_from(AiJobExecutionAttempt)) == 0


def test_legacy_unknown_remains_untouched_even_when_physical_proof_ended(recovery_setup):
    setup = recovery_setup
    identity = setup.reserve(legacy=True)
    job = setup.claim(identity)
    with setup.database.transaction() as session:
        QuotaService(session).finish(setup.user_id, identity, outcome="unknown", usage=USAGE)
    job.execution_lease.close()
    assert recover_job(setup.database, setup.settings, identity).reason == "legacy"
    with setup.database.transaction() as session:
        reservation = session.get(AiRequestReservation, identity)
        assert reservation.state == "unknown" and reservation.usage == USAGE
        assert reservation.slot_released_at is None
        assert session.get(AiJobRecoveryState, identity) is None
        assert session.scalar(select(func.count()).select_from(AiJobExecutionAttempt)) == 0


def test_complete_provider_receipt_needs_explicit_repeat_fee_policy(recovery_setup):
    setup = recovery_setup
    identity = setup.reserve()
    job = setup.claim(identity)
    with setup.database.transaction() as session:
        reservation = session.get(AiRequestReservation, identity)
        reservation.provider_attempts = 1
        reservation.usage = USAGE
    job.execution_lease.close()
    assert recover_job(setup.database, setup.settings, identity).reason == "model_repeat_required"
    result = recover_job(setup.database, setup.settings, identity, allow_model_repeat=True)
    assert result.decision == "requeued" and result.reason == "model_repeat"
    with setup.database.transaction() as session:
        assert session.get(AiRequestReservation, identity).provider_attempts == 1
        assert session.get(AiRequestReservation, identity).usage == USAGE
        assert session.get(AiJobExecutionAttempt, (identity, 1)).provider_attempts == 1


def test_trusted_completed_checkpoint_recovers_without_consent_or_resetting_expired_deadline(recovery_setup):
    setup = recovery_setup
    identity = setup.reserve()
    job = setup.claim(identity)
    with setup.database.transaction() as session:
        session.get(AiRequestReservation, identity).provider_attempts = 1
        state = session.get(AiJobRecoveryState, identity)
        state.budget_started_at -= timedelta(hours=1)
        state.deadline_at -= timedelta(hours=1)
        expired_deadline = state.deadline_at
    job.execution_lease.close()
    assert recover_job(setup.database, setup.settings, identity).reason == "deadline"
    assert recover_job(setup.database, setup.settings, identity, checkpoint_available=True).reason == "checkpoint"
    second = setup.claim(identity)
    with setup.database.transaction() as session:
        assert session.get(AiJobRecoveryState, identity).deadline_at == expired_deadline
        assert recovery_budget(session, identity, second.execution_lease.token, allow_expired=True)
    with pytest.raises(RecoveryBudgetExceeded), setup.database.transaction() as session:
        charge_recovery_step(session, identity, second.execution_lease.token, kind="model")


def test_concurrent_recovery_requeues_once_and_preserves_one_snapshot(recovery_setup):
    setup = recovery_setup
    identity = setup.reserve()
    setup.claim(identity).execution_lease.close()
    barrier = Barrier(3)

    def recover(_):
        barrier.wait(timeout=10)
        return recover_job(setup.database, setup.settings, identity)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(recover, range(3)))
    assert sum(result.decision == "requeued" for result in results) == 1
    with setup.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiJobExecutionAttempt)) == 1
        assert session.scalar(select(func.count()).select_from(AiJobOutbox)) == 2


def test_recovery_failure_rolls_back_archive_fence_and_queue_transition(recovery_setup, monkeypatch):
    from project_copilot.services import messaging
    setup = recovery_setup
    identity = setup.reserve()
    original = setup.claim(identity)
    original.execution_lease.close()

    def fail_enqueue(*_args, **_kwargs):
        raise SQLAlchemyError("synthetic recovery dispatch rollback")

    monkeypatch.setattr(messaging, "enqueue_outbox", fail_enqueue)
    with pytest.raises(SQLAlchemyError):
        recover_job(setup.database, setup.settings, identity)
    with setup.database.transaction() as session:
        assert session.get(AiRequestReservation, identity).state == "running"
        assert session.get(AiJobExecution, identity).fenced_at is None
        assert session.get(AiJobRecoveryState, identity).current_attempt_no == 1
        assert session.scalar(select(func.count()).select_from(AiJobExecutionAttempt)) == 0


@pytest.mark.parametrize("change", ["logout", "closed", "new_grant"])
def test_latest_session_release_and_grant_are_revalidated(recovery_setup, change):
    setup = recovery_setup
    identity = setup.reserve()
    setup.claim(identity).execution_lease.close()
    with setup.database.transaction() as session:
        if change == "logout":
            AuthService(session).logout(session.get(LoginSession, setup.login_id))
        else:
            release.change_state(session, setup.settings, actor_id=setup.admin_id, operation="CLOSED",
                                 reason="synthetic recovery close")
            if change == "new_grant":
                release.change_state(session, setup.settings, actor_id=setup.admin_id, operation="PERSONAL_DEMO",
                    personal_user_id=setup.user_id, duration_minutes=90, reason="synthetic replacement grant")
                release.change_state(session, setup.settings, actor_id=setup.admin_id, operation="worker-enable",
                                     reason="synthetic new grant worker")
    result = recover_job(setup.database, setup.settings, identity)
    assert result.reason == ("authorization" if change == "logout" else "release_closed")
    with setup.database.transaction() as session:
        assert session.get(AiRequestReservation, identity).state in {"running", "unknown"}
        assert session.get(AiJobRecoveryState, identity).current_attempt_no == 1


def test_logical_proposal_and_ticket_identity_survive_execution_attempt_change(recovery_setup):
    setup = recovery_setup
    with setup.database.transaction() as session:
        principal, _ = AuthService(session).authenticate(setup.token)
        context = ApprovalService(session, principal=principal, enforce_ownership=True).create_context()
    identity = setup.reserve("agent", context_id=context.context_id)
    first = setup.claim(identity)
    backend = ProposalBackend(setup.settings, setup.database, context.context_id, None,
                              auth_session_id=setup.login_id)
    tracker = ExecutionTracker(setup.database, identity, first.execution_lease.token)
    with track_execution(tracker):
        preview = asyncio.run(backend.propose_ticket("stable proposal", "same logical business operation"))
    tracker.drain()
    action_id = UUID(preview["data"]["action_id"])
    first.execution_lease.close()
    assert recover_job(setup.database, setup.settings, identity).decision == "requeued"
    second = setup.claim(identity)
    tracker = ExecutionTracker(setup.database, identity, second.execution_lease.token)
    with track_execution(tracker):
        repeated = asyncio.run(backend.propose_ticket("stable proposal", "same logical business operation"))
        with pytest.raises(ExecutionFenced):
            asyncio.run(backend.propose_ticket("changed proposal", "must not create another action"))
    tracker.drain()
    assert repeated["data"]["action_id"] == str(action_id)
    with setup.database.transaction() as session:
        principal, _ = AuthService(session).authenticate(setup.token)
        service = ApprovalService(session, principal=principal, enforce_ownership=True)
        owned = service.authenticate_owned(context.context_id)
        action = session.get(PendingAction, action_id)
        first_confirm = service.decide(owned, action_id, action.payload_fingerprint, approve=True)
        second_confirm = service.decide(owned, action_id, action.payload_fingerprint, approve=True)
        assert first_confirm.ticket_id == second_confirm.ticket_id
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 1
        assert session.scalar(select(func.count()).select_from(Ticket)) == 1
        assert session.get(JobBusinessOperation, (identity, "propose_ticket")).action_id == action_id
        assert session.get(Ticket, first_confirm.ticket_id).creation_key == action_id
    with pytest.raises(DBAPIError), setup.database.transaction() as session:
        session.execute(text("UPDATE job_business_operations SET payload_fingerprint=:value WHERE job_id=:identity"),
                        {"value": "a" * 64, "identity": identity})
    with pytest.raises(DBAPIError), setup.database.transaction() as session:
        session.execute(text("DELETE FROM ai_job_execution_attempts WHERE job_id=:identity"), {"identity": identity})


def test_bounded_sweep_ignores_legacy_and_disabled_recovery(recovery_setup):
    setup = recovery_setup
    identity = setup.reserve()
    setup.claim(identity).execution_lease.close()
    disabled = setup.settings.model_copy(update={"job_recovery_enabled": False})
    assert recover_pending(setup.database, disabled) == []
    assert recover_job(setup.database, disabled, identity).reason == "disabled"
    result = recover_pending(setup.database, setup.settings, limit=1)
    assert len(result) == 1 and result[0].decision == "requeued"


def test_mq_redelivery_automatically_recovers_safe_ended_job_then_executes_once(recovery_setup):
    from project_copilot.services.mq_worker import process_message
    setup = recovery_setup
    identity = setup.reserve()
    abandoned = setup.claim(identity)
    abandoned.execution_lease.close()
    with setup.database.transaction() as session:
        original_event = session.scalar(select(AiJobOutbox).where(AiJobOutbox.job_id == identity))
        original = JobMessage(original_event.event_id, identity, original_event.dispatch_generation)
    calls = []

    def runner(_settings, _database, job):
        calls.append(job.id)
        return SimpleNamespace(model_dump=lambda **_: {"answer": "recovered controlled result"}, generation=None)

    assert process_message(setup.database, setup.settings, original, runner=runner) == "deferred"
    assert calls == []
    with setup.database.transaction() as session:
        second_event = session.scalar(select(AiJobOutbox).where(
            AiJobOutbox.job_id == identity, AiJobOutbox.dispatch_generation == 2))
        second = JobMessage(second_event.event_id, identity, 2)
    assert process_message(setup.database, setup.settings, second, runner=runner) == "completed"
    assert process_message(setup.database, setup.settings, second, runner=runner) == "completed"
    assert calls == [identity]
    with setup.database.transaction() as session:
        reservation = session.get(AiRequestReservation, identity)
        assert reservation.state == "succeeded" and reservation.provider_attempts == 0
        assert session.get(AiJobRecoveryState, identity).current_attempt_no == 2
        assert session.get(AiJob, identity).result == {"answer": "recovered controlled result"}
        assert session.scalar(select(func.count()).select_from(AiJobExecutionAttempt)) == 1
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 1


def test_bounded_sweep_visits_later_jobs_after_twenty_blocked_candidates(recovery_setup):
    setup = recovery_setup
    blocked = []
    for _ in range(20):
        identity = setup.reserve()
        job = setup.claim(identity)
        with setup.database.transaction() as session:
            # Synthetic fully observed provider initiation, not an HTTP/model call.
            session.get(AiRequestReservation, identity).provider_attempts = 1
        job.execution_lease.close()
        blocked.append(identity)
    eligible = setup.reserve()
    setup.claim(eligible).execution_lease.close()
    first = recover_pending(setup.database, setup.settings, limit=20)
    assert len(first) == 20 and all(result.reason == "model_repeat_required" for result in first)
    with setup.database.transaction() as session:
        assert session.get(AiJobRecoveryState, eligible).last_inspected_at is None
    second = recover_pending(setup.database, setup.settings, limit=20)
    assert len(second) == 20 and second[0].decision == "requeued"
    with setup.database.transaction() as session:
        assert session.get(AiJobRecoveryState, eligible).current_attempt_no == 2
        assert session.get(AiRequestReservation, eligible).state == "reserved"
        assert all(session.get(AiJobRecoveryState, identity).current_attempt_no == 1 for identity in blocked)


def test_operator_terminal_no_retry_decision_prevents_automatic_recovery(recovery_setup):
    setup = recovery_setup
    identity = setup.reserve()
    setup.claim(identity).execution_lease.close()
    with setup.database.transaction() as session:
        reservation = session.get(AiRequestReservation, identity)
        reservation.state, reservation.finished_at = "unknown", None
        proof = session.get(AiJobExecution, identity)
        proof.fenced_at = session.scalar(select(func.clock_timestamp()))
        proof.release_reason, reservation.slot_released_at = "manual", proof.fenced_at
        session.add(JobRecoveryAudit(job_id=identity, actor_user_id=setup.admin_id,
            previous_state="unknown", resolution="terminal_no_retry", provider_attempts=0,
            evidence_sha256="a" * 64, inspection_sha256="b" * 64))
    decision = recover_job(setup.database, setup.settings, identity, allow_model_repeat=True,
                           checkpoint_available=True)
    assert decision.decision == "unchanged" and decision.reason == "not_recoverable"
    with setup.database.transaction() as session:
        assert session.get(AiRequestReservation, identity).state == "unknown"
        assert session.get(AiJobRecoveryState, identity).current_attempt_no == 1
        assert session.scalar(select(func.count()).select_from(AiJobExecutionAttempt)) == 0

