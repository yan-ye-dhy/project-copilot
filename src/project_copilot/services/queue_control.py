"""PostgreSQL backpressure; never infers completion from an execution age."""
from sqlalchemy import func, select, text

from project_copilot.db.models import AiRequestReservation
from project_copilot.infrastructure.tracing import trace_span
from project_copilot.services.quotas import QuotaExceeded

QUOTA_LOCK = 576572591122


def check_enqueue(session, settings):
    """Called under the existing quota lock, after resolving idempotent replay."""
    maximum = getattr(settings, "queue_max_pending_jobs", 0)
    if not maximum:
        return
    count = session.scalar(select(func.count()).select_from(AiRequestReservation).where(
        AiRequestReservation.state == "reserved"))
    with trace_span("queue.admission", {"queue.pending": count, "queue.capacity": maximum}) as span:
        span.set_attribute("queue.rejected", count >= maximum)
    if count >= maximum:
        raise QuotaExceeded("queue_backlog")


def lock_claim_capacity(session, settings):
    if getattr(settings, "worker_max_active_jobs", 0):
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": QUOTA_LOCK})


def can_claim(session, settings):
    """Acquire capacity lock BEFORE candidate rows, matching enqueue lock order.

    Unknown or failed work with an unreleased physical slot still counts. The
    existing execution-proof protocol alone may release it; Redis cannot.
    """
    maximum = getattr(settings, "worker_max_active_jobs", 0)
    if not maximum:
        return True
    lock_claim_capacity(session, settings)
    active = session.scalar(select(func.count()).select_from(AiRequestReservation).where(
        AiRequestReservation.state != "reserved", AiRequestReservation.slot_released_at.is_(None)))
    with trace_span("queue.capacity", {"active_execution_slots": active, "queue.execution_capacity": maximum}) as span:
        span.set_attribute("queue.claim_blocked", active >= maximum)
    return active < maximum


def inspect_failures(session, *, limit=20):
    """Bounded dead-letter-style read model: no payload, retry or mutation."""
    from project_copilot.db.jobs import AiJob
    from project_copilot.db.executions import AiJobExecution
    from project_copilot.services.job_failures import safe_failure
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid inspection bound")
    rows = session.execute(select(AiRequestReservation, AiJob.failure_type, AiJobExecution)
        .join(AiJob, AiJob.id == AiRequestReservation.id)
        .outerjoin(AiJobExecution, AiJobExecution.job_id == AiRequestReservation.id)
        .where(AiRequestReservation.state.in_(["failed", "unknown"]))
        .order_by(AiRequestReservation.created_at.desc(), AiRequestReservation.id).limit(limit)).all()
    return [{"job_id": str(row.id), "kind": row.kind, "state": row.state,
             "created_at": row.created_at.isoformat(), "failure_type": safe_failure(failure) if failure else None,
             "provider_attempts": row.provider_attempts,
             "provider_open": execution.provider_open if execution else None,
             "provider_uncertain": execution.provider_uncertain if execution else None,
             "slot_held": row.slot_released_at is None,
             "has_action": execution.action_id is not None if execution else None,
             "automatic_replay_allowed": False}
            for row, failure, execution in rows]
