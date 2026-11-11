"""Proof-based recovery for newly opted-in personal jobs, never historical replay."""
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select, text

from project_copilot.db.executions import AiJobExecution
from project_copilot.db.jobs import AiJob
from project_copilot.db.models import AiRequestReservation
from project_copilot.db.operations import JobContentTombstone, JobRecoveryAudit
from project_copilot.db.recovery import AiJobExecutionAttempt, AiJobRecoveryState, AiJobStep, JobBusinessOperation
from project_copilot.services.execution_slots import ExecutionLease, _business_valid, _locked, guard_execution

RECOVERY_REASONS = frozenset({"disabled", "legacy", "not_personal", "not_recoverable", "attempt_limit",
    "deadline", "budget", "authorization", "release_closed", "missing_proof", "live_or_invalid_proof",
    "provider_open", "provider_uncertain", "business_binding", "model_repeat_required", "capacity",
    "metadata_mismatch", "no_provider", "checkpoint", "model_repeat", "already_queued"})


@dataclass(frozen=True)
class RecoveryDecision:
    decision: str
    reason: str
    attempt_no: int | None = None


class RecoveryBudgetExceeded(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("recovery execution budget exhausted")


def _enabled(settings):
    return bool(getattr(settings, "job_recovery_enabled", False) and settings.message_queue_enabled)


def initialize_recovery(session, job_id, settings):
    """Only an API's new-job transaction calls this; existing jobs are not backfilled."""
    if not _enabled(settings):
        return None
    reservation = session.get(AiRequestReservation, job_id)
    job = session.get(AiJob, job_id)
    if (job is None or reservation is None or reservation.personal_demo_grant_id is None
            or reservation.kind not in {"rag", "agent"} or reservation.state != "reserved"
            or reservation.provider_attempts != 0 or session.get(AiJobExecution, job_id) is not None):
        return None
    existing = session.get(AiJobRecoveryState, job_id)
    if existing is not None:
        return existing
    row = AiJobRecoveryState(job_id=job_id,
        timeout_seconds=settings.personal_agent_total_timeout_seconds,
        max_model_calls=settings.personal_agent_max_model_calls,
        max_tool_calls=settings.personal_agent_max_tool_calls,
        max_provider_attempts=settings.personal_max_provider_attempts_per_job)
    session.add(row)
    session.flush()
    return row


def start_recovery_budget(session, job_id, settings):
    """Start once, after queue wait. Subsequent claims retain the original deadline."""
    row = session.get(AiJobRecoveryState, job_id, with_for_update=True)
    if row is None:
        return None
    if row.budget_started_at is None:
        row.budget_started_at = session.scalar(select(func.clock_timestamp()))
        row.deadline_at = row.budget_started_at + timedelta(seconds=row.timeout_seconds)
    session.flush()
    return row


def revalidate_job_authority(session, settings, job_id):
    """Recheck current authorization before reading cached content or resuming work."""
    from project_copilot.services.approvals import ApprovalService, ContextRejected
    from project_copilot.services.auth import AuthService, AuthenticationRejected
    from project_copilot.services.release_control import require_open
    job = session.get(AiJob, job_id)
    reservation = session.get(AiRequestReservation, job_id)
    if job is None or reservation is None:
        raise AuthenticationRejected()
    require_open(session, settings, user_id=reservation.user_id, worker=True, reservation=reservation)
    principal, _ = AuthService(session).authenticate_session_id(job.login_session_id, for_update=True)
    if principal.id != reservation.user_id:
        raise AuthenticationRejected()
    if job.context_id is not None:
        if reservation.kind != "agent":
            raise ContextRejected()
        ApprovalService(session, principal=principal, enforce_ownership=True).authenticate_owned(job.context_id)
    return job, reservation


def recovery_budget(session, job_id, token, *, allow_expired=False):
    """Called in the same fenced transaction as a new external operation intent."""
    guard_execution(session, job_id, token)
    state = session.get(AiJobRecoveryState, job_id, with_for_update=True)
    if state is None:
        return None
    if state.deadline_at is None:
        raise RecoveryBudgetExceeded("metadata_mismatch")
    if not allow_expired and session.scalar(select(func.clock_timestamp())) >= state.deadline_at:
        raise RecoveryBudgetExceeded("deadline")
    return state


def charge_recovery_step(session, job_id, token, *, kind):
    if kind not in {"model", "tool"}:
        raise ValueError("invalid recovery charge kind")
    state = recovery_budget(session, job_id, token)
    if state is None:
        return 0
    field = "model_calls" if kind == "model" else "tool_calls"
    maximum = state.max_model_calls if kind == "model" else state.max_tool_calls
    count = getattr(state, field)
    if count >= maximum:
        raise RecoveryBudgetExceeded(kind + "_budget")
    setattr(state, field, count + 1)
    session.flush()
    return count + 1


def _decision(session, state, reason, *, decision="blocked"):
    if reason not in RECOVERY_REASONS:
        raise ValueError("invalid recovery reason")
    if state is not None:
        state.blocked_reason = reason if decision == "blocked" else None
        state.last_inspected_at = session.scalar(select(func.clock_timestamp()))
    return RecoveryDecision(decision, reason, state.current_attempt_no if state is not None else None)


def _capacity_available(session, settings, reservation):
    from project_copilot.services.personal_profile import personal_limits
    from project_copilot.services.queue_control import check_enqueue
    from project_copilot.services.quotas import QuotaExceeded
    try:
        check_enqueue(session, settings)
    except QuotaExceeded:
        return False
    if reservation.slot_released_at is None:
        return True
    held = session.scalar(select(func.count()).select_from(AiRequestReservation).where(
        AiRequestReservation.user_id == reservation.user_id,
        AiRequestReservation.id != reservation.id,
        AiRequestReservation.slot_released_at.is_(None)))
    return held < personal_limits(settings)["concurrent_limit"]


def _checkpoint_bindings(settings):
    # This helper only reads the atomically published manifest. It must never
    # acquire an index flock, contact a provider, or construct/warm a model while
    # this transaction holds the job's PostgreSQL rows.
    from project_copilot.infrastructure.recovery_runtime import checkpoint_bindings
    return checkpoint_bindings(settings)


def _saved_checkpoint_available(session, settings, job, execution, state):
    """Receipts admit replay; fresh authorization and resource checks still apply.

    Every initiated step must have a complete, matching receipt. Without a
    per-step provider initiation ledger, one completed earlier model cannot
    prove that a later unfinished intent is free to repeat. A mapped proposal
    is independent proof: its original action can be read without running the
    graph or repeating the model which requested it.
    """
    operation = session.get(JobBusinessOperation, (job.id, "propose_ticket"))
    if operation is not None:
        return operation.action_id == execution.action_id
    from project_copilot.services.job_steps import (
        MAX_JOB_STEPS, bounded_json, fingerprint, normalize_tool_result, restore_model_message,
    )
    rows = session.scalars(select(AiJobStep).where(AiJobStep.job_id == job.id).limit(MAX_JOB_STEPS + 1)).all()
    if (not rows or len(rows) > MAX_JOB_STEPS or not any(row.kind in {"model", "tool"} for row in rows)
            or any(row.status != "completed" for row in rows)):
        return False
    try:
        config, index = _checkpoint_bindings(settings)
        for row in rows:
            if (row.config_sha256 != config or row.index_fingerprint != index
                    or row.execution_token != execution.token or row.attempt_no != state.current_attempt_no):
                return False
            if row.kind == "model":
                restore_model_message(row.result)
            elif row.kind == "tool":
                normalize_tool_result(row.result)
            else:
                bounded_json(row.result)
            if row.result_sha256 != fingerprint(row.result):
                return False
    except Exception:
        # A missing/changed resource or malformed receipt is not retry proof.
        # No exception text, file content or provider data crosses this boundary.
        return False
    return True


def recover_job(database, settings, job_id, *, allow_model_repeat=False, checkpoint_available=None):
    """Inspect a redelivery and requeue only proven-ended, eligible protocol-v2 work.

    By default complete receipts or a verified stable proposal supply replay
    proof. checkpoint_available is an optional trusted backend/test override;
    it is never an HTTP parameter. Neither it nor consent overrides uncertainty.
    All old execution facts are retained in an immutable row before current proof
    is fenced. The later claim rotates that pointer only after verifying the row.
    """
    if (type(job_id) is not UUID or type(allow_model_repeat) is not bool
            or (checkpoint_available is not None and type(checkpoint_available) is not bool)):
        raise ValueError("invalid recovery request")
    if not _enabled(settings):
        return RecoveryDecision("unchanged", "disabled")
    lease = None
    try:
        with database.transaction() as session:
            state = session.get(AiJobRecoveryState, job_id)
            if state is None:
                return RecoveryDecision("unchanged", "legacy")
            from project_copilot.services.approvals import ContextRejected
            from project_copilot.services.auth import AuthenticationRejected
            from project_copilot.services.release_control import ReleaseClosed
            try:
                job, candidate = revalidate_job_authority(session, settings, job_id)
            except ReleaseClosed:
                return _decision(session, state, "release_closed")
            except (AuthenticationRejected, ContextRejected):
                return _decision(session, state, "authorization")
            # Match reservation/claim capacity order before taking physical rows.
            session.execute(text("SELECT pg_advisory_xact_lock(576572591122)"))
            reservation, execution = _locked(session, job_id)
            state = session.get(AiJobRecoveryState, job_id, with_for_update=True, populate_existing=True)
            if (reservation.personal_demo_grant_id is None or reservation.kind not in {"rag", "agent"}):
                return _decision(session, state, "not_personal")
            if reservation.state == "reserved":
                return _decision(session, state, "already_queued", decision="unchanged")
            if reservation.state not in {"running", "unknown"} or job.result is not None:
                return _decision(session, state, "not_recoverable", decision="unchanged")
            if (session.get(JobContentTombstone, job_id) is not None
                    or session.scalar(select(JobRecoveryAudit.id).where(JobRecoveryAudit.job_id == job_id))
                    or (execution is not None and execution.release_reason == "manual")):
                return _decision(session, state, "not_recoverable", decision="unchanged")
            if state.current_attempt_no >= min(2, getattr(settings, "job_recovery_max_attempts", 2)):
                return _decision(session, state, "attempt_limit")
            if state.deadline_at is None:
                return _decision(session, state, "metadata_mismatch")
            now = session.scalar(select(func.clock_timestamp()))
            if execution is None:
                return _decision(session, state, "missing_proof")
            if execution.provider_open:
                return _decision(session, state, "provider_open")
            if execution.provider_uncertain:
                return _decision(session, state, "provider_uncertain")
            if not _business_valid(session, reservation, execution):
                return _decision(session, state, "business_binding")
            lease = ExecutionLease.try_acquire(settings.manifest_dir, job_id, execution.token, execution.lease_inode)
            if lease is None:
                return _decision(session, state, "live_or_invalid_proof")
            if not lease.valid_owned():
                return _decision(session, state, "live_or_invalid_proof")
            if checkpoint_available is None:
                checkpoint_available = _saved_checkpoint_available(session, settings, job, execution, state)
            if now >= state.deadline_at and not checkpoint_available:
                return _decision(session, state, "deadline")
            paid = reservation.provider_attempts > state.attempt_provider_baseline
            if paid and not checkpoint_available and not allow_model_repeat:
                return _decision(session, state, "model_repeat_required")
            if (not checkpoint_available and (reservation.provider_attempts >= state.max_provider_attempts
                    or state.model_calls >= state.max_model_calls)):
                return _decision(session, state, "budget")
            if not _capacity_available(session, settings, reservation):
                return _decision(session, state, "capacity")
            # The quota lock wait can cross a session/context/grant deadline.
            # Retake the same authorization checks before changing durable work.
            try:
                revalidate_job_authority(session, settings, job_id)
            except ReleaseClosed:
                return _decision(session, state, "release_closed")
            except (AuthenticationRejected, ContextRejected):
                return _decision(session, state, "authorization")
            reason = "checkpoint" if checkpoint_available else "model_repeat" if paid else "no_provider"
            previous = reservation.state
            if execution.fenced_at is None:
                execution.fenced_at = now
                execution.release_reason = "dead_readonly" if reservation.kind == "rag" else "dead_agent_fenced"
            snapshot = AiJobExecutionAttempt(job_id=job_id, attempt_no=state.current_attempt_no,
                token=execution.token, lease_inode=execution.lease_inode, provider_open=execution.provider_open,
                provider_uncertain=execution.provider_uncertain, provider_baseline=state.attempt_provider_baseline,
                provider_attempts=reservation.provider_attempts, action_id=execution.action_id,
                local_done_at=execution.local_done_at, fenced_at=execution.fenced_at,
                release_reason=execution.release_reason, execution_created_at=execution.created_at,
                previous_state=previous, usage=reservation.usage,
                model_calls=state.model_calls, tool_calls=state.tool_calls, reason=reason)
            session.add(snapshot)
            session.flush()  # Failure here rolls back fencing and the eventual queue transition.
            state.current_attempt_no += 1
            state.attempt_provider_baseline = reservation.provider_attempts
            state.event_floor, state.blocked_reason = job.event_sequence, None
            state.last_inspected_at = now
            reservation.state, reservation.finished_at, reservation.slot_released_at = "reserved", None, None
            from project_copilot.services.messaging import enqueue_outbox
            from project_copilot.db.messaging import AiJobOutbox
            message = enqueue_outbox(session, job_id, dispatch_generation=state.current_attempt_no)
            session.get(AiJobOutbox, message.event_id).available_at = session.scalar(select(func.clock_timestamp())) + timedelta(
                seconds=getattr(settings, "job_recovery_retry_delay_seconds", 5))
            return RecoveryDecision("requeued", reason, state.current_attempt_no)
    finally:
        if lease is not None:
            lease.close()


def authorize_step_retry(session, state, step):
    """Only an immutable recovery decision can admit a prior unfinished intent."""
    if (state is None or step.status == "completed" or step.attempt_no >= state.current_attempt_no
            or step.prior_intent is not None):
        return False
    proof = session.get(AiJobExecutionAttempt, (state.job_id, step.attempt_no))
    if (proof is None or proof.token != step.execution_token or proof.provider_open != 0
            or proof.provider_uncertain):
        return False
    if proof.reason == "no_provider":
        return proof.provider_attempts == proof.provider_baseline
    # StepJournal additionally requires its explicit readonly_tool retry policy
    # for tools. A proposal is reconciled through its stable operation instead.
    return proof.reason == "model_repeat" and step.kind in {"model", "tool"}


def recover_pending(database, settings, *, limit=20):
    """Bounded maintenance of new protocol jobs, using the same proof coordinator."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid recovery inspection batch")
    if not _enabled(settings):
        return []
    with database.transaction() as session:
        identities = session.scalars(select(AiJobRecoveryState.job_id)
            .join(AiRequestReservation, AiRequestReservation.id == AiJobRecoveryState.job_id)
            .where(AiRequestReservation.state.in_(("running", "unknown")))
            .order_by(AiJobRecoveryState.last_inspected_at.asc().nulls_first(),
                      AiJobRecoveryState.created_at, AiJobRecoveryState.job_id).limit(limit)).all()
    return [recover_job(database, settings, job_id,
        allow_model_repeat=getattr(settings, "job_recovery_allow_model_repeat", False)) for job_id in identities]
