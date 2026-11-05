"""Explicit administrator maintenance; uncertain jobs are never retried.

Retention deliberately keeps proposals, tickets, reservation identities, recovery
records and unknown/running payloads. WAL and backups have separate retention.
"""
from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
import json
import re
from uuid import UUID

from sqlalchemy import exists, func, select, text

from project_copilot.db.jobs import AiJob, AiJobUpload, AiRequestAttempt, AiJobEvent
from project_copilot.db.models import AgentContext, AiRequestReservation, PendingAction, Ticket, UserAccount
from project_copilot.db.operations import JobContentTombstone, JobRecoveryAudit, MaintenanceAudit

# Shared with services.release_control. A worker holds WORKER_EXECUTION_LOCK
# throughout execution; closing ingress alone is insufficient to prove quiescence.
RELEASE_ADMISSION_LOCK = 604116220001
WORKER_EXECUTION_LOCK = 604116220002


class MaintenanceRejected(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _require_admin(session, actor_user_id):
    actor = session.scalar(select(UserAccount).where(UserAccount.id == actor_user_id,
        UserAccount.role == "admin", UserAccount.active.is_(True)).with_for_update(read=True))
    if actor is None:
        raise MaintenanceRejected("active administrator required")


@contextmanager
def closed_quiescent(session):
    """No claim/provider admission or release transition can race this operation.

    The nonblocking transaction lock also proves every cooperating worker has
    released its execution lease. Operators must additionally verify containers
    and upstream completion; database locks cannot inspect a remote provider.
    """
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": RELEASE_ADMISSION_LOCK}):
        raise MaintenanceRejected("release admission is busy")
    from project_copilot.services.release_control import ReleaseClosed, require_closed
    try:
        require_closed(session)
    except ReleaseClosed:
        raise MaintenanceRejected("maintenance requires CLOSED with worker disabled") from None
    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": WORKER_EXECUTION_LOCK}):
        raise MaintenanceRejected("an execution lease is still active")
    yield


def cleanup(session, actor_user_id: UUID, *, batch_size: int = 100):
    """Bounded explicit cleanup; caller commits audit and changes atomically."""
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError("invalid maintenance batch")
    _require_admin(session, actor_user_id)
    with closed_quiescent(session):
        now = session.scalar(select(func.clock_timestamp()))
        counts = {"expired_unclaimed": 0, "expired_uploads": 0, "terminal_content": 0, "request_audits": 0, "unreferenced_contexts": 0}
        # An unclaimed job is safe to expire; ever-claimed work is excluded.
        reservations = session.scalars(select(AiRequestReservation).where(
            AiRequestReservation.state == "reserved", AiRequestReservation.created_at < now - timedelta(hours=24))
            .order_by(AiRequestReservation.created_at).with_for_update(skip_locked=True).limit(batch_size)).all()
        for reservation in reservations:
            attachment = session.get(AiJobUpload, reservation.id)
            if attachment is not None:
                session.delete(attachment)
                counts["expired_uploads"] += 1
            job = session.get(AiJob, reservation.id)
            if job is not None:
                job.payload, job.result = {}, None
            reservation.state, reservation.finished_at = "failed", now
            reservation.slot_released_at = now
            counts["expired_unclaimed"] += 1

        rows = session.execute(select(AiJob, AiRequestReservation).join(AiRequestReservation).where(
            AiRequestReservation.state.in_(("succeeded", "failed")),
            AiRequestReservation.finished_at < now - timedelta(days=7),
            ~exists().where(JobRecoveryAudit.job_id == AiJob.id),
            ~exists().where(JobContentTombstone.job_id == AiJob.id),
        ).order_by(AiRequestReservation.finished_at).with_for_update(of=AiRequestReservation, skip_locked=True)
            .limit(batch_size)).all()
        for job, reservation in rows:
            job.payload, job.result = {}, None
            attachment = session.get(AiJobUpload, job.id)
            if attachment is not None:
                session.delete(attachment)
            session.add(JobContentTombstone(job_id=job.id, cleared_at=now))
            counts["terminal_content"] += 1

        # Expiring display drafts is distinct from deleting uncertain execution evidence.
        old_events = session.scalars(select(AiJobEvent).join(AiJob).join(AiRequestReservation).where(
            AiJobEvent.created_at < now - timedelta(days=7),
            AiRequestReservation.state.in_(("succeeded", "failed")),
            ~exists().where(JobRecoveryAudit.job_id == AiJobEvent.job_id),
        ).order_by(AiJobEvent.created_at).with_for_update(of=AiJobEvent, skip_locked=True)
            .limit(batch_size)).all()
        for event in old_events:
            session.delete(event)
        counts["event_content"] = len(old_events)

        old_audits = session.scalars(select(AiRequestAttempt).where(
            AiRequestAttempt.created_at < now - timedelta(days=7),
            # Keep attempt evidence for uncertain or manually resolved tasks.
            ~exists().where(AiRequestReservation.id == AiRequestAttempt.reservation_id,
                AiRequestReservation.state.in_(("running", "unknown"))),
            ~exists().where(JobRecoveryAudit.job_id == AiRequestAttempt.reservation_id),
        ).order_by(AiRequestAttempt.created_at).with_for_update(skip_locked=True).limit(batch_size)).all()
        for row in old_audits:
            session.delete(row)
            counts["request_audits"] += 1

        contexts = session.scalars(select(AgentContext).where(
            AgentContext.owner_user_id.is_not(None),
            AgentContext.expires_at < now - timedelta(days=7),
            ~exists().where(PendingAction.context_id == AgentContext.id),
            ~exists().where(AiJob.context_id == AgentContext.id),
        ).order_by(AgentContext.expires_at).with_for_update(skip_locked=True).limit(batch_size)).all()
        for context in contexts:
            session.delete(context)
            counts["unreferenced_contexts"] += 1
        session.add(MaintenanceAudit(actor_user_id=actor_user_id, counts=counts))
        session.flush()
        return counts


def inspect_job(session, actor_user_id: UUID, job_id: UUID) -> dict:
    """Read-only, sanitized investigation. No prompts, cookies or raw tool data."""
    actor = session.scalar(select(UserAccount.id).where(UserAccount.id == actor_user_id,
        UserAccount.role == "admin", UserAccount.active.is_(True)))
    if actor is None:
        raise MaintenanceRejected("active administrator required")
    row = session.execute(select(AiJob, AiRequestReservation).join(AiRequestReservation)
        .where(AiJob.id == job_id)).one_or_none()
    if row is None:
        raise MaintenanceRejected("job does not exist")
    job, reservation = row
    actions = session.scalars(select(PendingAction).where(PendingAction.context_id == job.context_id)
        .order_by(PendingAction.id)).all() if job.context_id is not None else []
    action_details = []
    for action in actions:
        ticket = session.get(Ticket, action.ticket_id) if action.ticket_id is not None else None
        action_details.append({"action_id": str(action.id), "status": action.status,
            "ticket_id": str(action.ticket_id) if action.ticket_id is not None else None,
            "binding_valid": bool(ticket and ticket.creation_key == action.id and ticket.owner_user_id == action.owner_user_id)
                if action.ticket_id is not None else True})
    result = {"job_id": str(job.id), "kind": reservation.kind, "state": reservation.state,
        "owner_user_id": str(reservation.user_id), "context_id": str(job.context_id) if job.context_id else None,
        "provider_attempts": reservation.provider_attempts, "usage": reservation.usage,
        "cost_status": reservation.cost_status, "created_at": reservation.created_at.isoformat(),
        "finished_at": reservation.finished_at.isoformat() if reservation.finished_at else None,
        "request_attempt_outcomes": [[outcome, count] for outcome, count in session.execute(
            select(AiRequestAttempt.outcome, func.count()).where(AiRequestAttempt.reservation_id == job.id)
            .group_by(AiRequestAttempt.outcome).order_by(AiRequestAttempt.outcome)).all()],
        "actions": action_details, "slot_held": reservation.slot_released_at is None,
        "slot_released_at": reservation.slot_released_at.isoformat() if reservation.slot_released_at else None,
        "manually_resolved": session.scalar(select(JobRecoveryAudit.id).where(JobRecoveryAudit.job_id == job.id)) is not None}
    from project_copilot.db.executions import AiJobExecution
    execution = session.get(AiJobExecution, job_id)
    result["execution"] = None if execution is None else {
        "token": str(execution.token), "provider_open": execution.provider_open,
        "provider_uncertain": execution.provider_uncertain,
        "local_done_at": execution.local_done_at.isoformat() if execution.local_done_at else None,
        "fenced_at": execution.fenced_at.isoformat() if execution.fenced_at else None,
        "release_reason": execution.release_reason,
    }
    result["inspection_sha256"] = sha256(_canonical(result).encode()).hexdigest()
    return result


def resolve_no_retry(session, actor_user_id: UUID, job_id: UUID, evidence: dict, *, worker_stop_proof=None, settings=None) -> dict:
    """Retire a verified stopped execution, retaining all original evidence.

    This is an administrator decision after checking process/container inventory,
    business rows and the provider. It does not claim the upstream call failed or
    reproduce its result; unknown stays unknown and only its concurrency slot ends.
    """
    required = {"version", "job_id", "inspection_sha256", "original_execution_terminated",
        "business_verified", "provider_outcome", "evidence_reference_sha256", "decision"}
    if (not isinstance(evidence, dict) or set(evidence) != required or evidence["version"] != 1 or
        evidence["job_id"] != str(job_id) or evidence["original_execution_terminated"] is not True or
        evidence["business_verified"] is not True or evidence["decision"] != "terminal_no_retry" or
        evidence["provider_outcome"] not in {"not_called", "completed_and_reconciled"} or
        any(not isinstance(evidence[key], str) or not re.fullmatch("[0-9a-f]{64}", evidence[key])
            for key in ("inspection_sha256", "evidence_reference_sha256"))):
        raise MaintenanceRejected("complete verified recovery evidence is required")
    _require_admin(session, actor_user_id)
    with closed_quiescent(session):
        reservation = session.scalar(select(AiRequestReservation).where(AiRequestReservation.id == job_id)
            .with_for_update())
        from project_copilot.db.executions import AiJobExecution
        execution = session.get(AiJobExecution, job_id, with_for_update=True)
        eligible = reservation is not None and (reservation.state in ("running", "unknown") or
            (reservation.state in ("succeeded", "failed") and execution is not None))
        if (not eligible
                or reservation.slot_released_at is not None):
            raise MaintenanceRejected("only held executions requiring reconciliation can be retired")
        from project_copilot.services.worker_stop import verify_inventory
        try:
            verify_inventory(worker_stop_proof, secret=settings.public_proxy_secret.get_secret_value(),
                job_id=job_id, job_created_at=reservation.created_at, resource_id=settings.public_resource_id,
                now=session.scalar(select(func.clock_timestamp())), review_sha256=evidence["evidence_reference_sha256"])
        except (ValueError, TypeError, AttributeError, KeyError):
            raise MaintenanceRejected("trusted fresh Docker worker-stop proof required") from None
        if reservation.provider_attempts and evidence["provider_outcome"] == "not_called":
            raise MaintenanceRejected("provider initiation must be reconciled")
        inspection = inspect_job(session, actor_user_id, job_id)
        if inspection["inspection_sha256"] != evidence["inspection_sha256"]:
            raise MaintenanceRejected("inspection changed; inspect again")
        if not all(item["binding_valid"] for item in inspection["actions"]):
            raise MaintenanceRejected("business binding is inconsistent")
        session.add(JobRecoveryAudit(job_id=job_id, actor_user_id=actor_user_id,
            previous_state=reservation.state, resolution="terminal_no_retry",
            provider_attempts=reservation.provider_attempts, prior_usage=reservation.usage,
            evidence_sha256=sha256(_canonical({"review": evidence, "worker_stop": worker_stop_proof}).encode()).hexdigest(),
            inspection_sha256=inspection["inspection_sha256"]))
        now = session.scalar(select(func.clock_timestamp()))
        if reservation.state == "running":
            reservation.state, reservation.finished_at = "unknown", None
        reservation.slot_released_at = now
        if execution is not None:
            execution.fenced_at, execution.release_reason = now, "manual"
        session.flush()
        return {"job_id": str(job_id), "state": reservation.state, "resolution": "terminal_no_retry",
                "slot_released": True, "replay_permitted": False, "provider_result": "not_inferred"}
