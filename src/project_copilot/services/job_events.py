"""A bounded PostgreSQL journal shared by independent Worker/API processes."""
from datetime import timedelta
import json
import logging
import time
from threading import RLock
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from project_copilot.db.jobs import AiJob, AiJobEvent
from project_copilot.db.models import AiRequestReservation
from project_copilot.db.recovery import AiJobRecoveryState
from project_copilot.infrastructure.job_progress import LABELS
from project_copilot.services.job_failures import SAFE_FAILURE_TYPES

MAX_EVENTS = 512
RETENTION_DAYS = 7
logger = logging.getLogger(__name__)


def execution_snapshot(state, status):
    """Public, read-only recovery metadata; no physical proof or private state."""
    if state is None or state.protocol_version != 2:
        return None
    from project_copilot.services.job_recovery import RECOVERY_REASONS

    reason = state.blocked_reason
    if reason is not None and reason not in RECOVERY_REASONS:
        reason = "metadata_mismatch"
    if status in {"succeeded", "failed"}:
        phase = "completed"
    elif status == "reserved":
        phase = "waiting_dispatch"
    elif status == "running":
        # A scan may have observed the original execution still alive. That
        # fact must not label an actively running task as blocked or failed.
        phase = "recovering" if state.current_attempt_no > 1 else "running"
    else:
        phase = "blocked" if reason else "awaiting_recovery"
    return {"attempt_no": state.current_attempt_no, "event_floor": state.event_floor,
            "recovery_status": phase, "blocked_reason": reason,
            "deadline_at": state.deadline_at.isoformat() if state.deadline_at is not None else None,
            "model_calls": state.model_calls, "tool_calls": state.tool_calls,
            "can_resume_waiting": phase not in {"completed", "blocked"}}


def execution_info(session, job_id):
    row = session.execute(select(AiJobRecoveryState, AiRequestReservation.state)
        .join(AiRequestReservation, AiRequestReservation.id == AiJobRecoveryState.job_id)
        .where(AiJobRecoveryState.job_id == job_id)).one_or_none()
    return execution_snapshot(*row) if row is not None else None


def _event_floor(job_id):
    # Read the floor in the same SQL snapshot as the events. Jobs that never
    # opted into recovery retain their existing journal and response contract.
    return func.coalesce(select(AiJobRecoveryState.event_floor)
        .where(AiJobRecoveryState.job_id == job_id).scalar_subquery(), 0)


def append_event(session, job_id: UUID, kind: str, data: dict):
    """Serialize sequence assignment on the job row, in the caller's transaction."""
    if kind == "progress":
        if set(data) != {"stage", "label"} or data.get("stage") not in LABELS or data["label"] != LABELS[data["stage"]]:
            raise ValueError("invalid progress")
    elif kind == "delta":
        if set(data) != {"text"} or not isinstance(data["text"], str) or not 1 <= len(data["text"]) <= 1024:
            raise ValueError("invalid delta")
    elif kind == "terminal":
        if set(data) != {"status"} or data["status"] not in {"succeeded", "failed", "unknown"}:
            raise ValueError("invalid terminal")
    elif kind == "diagnostic":
        if set(data) != {"failure_type"} or data["failure_type"] not in SAFE_FAILURE_TYPES:
            raise ValueError("invalid diagnostic")
    else:
        raise ValueError("invalid event")
    if len(json.dumps(data, ensure_ascii=True).encode()) > 8192:
        raise ValueError("event too large")
    job = session.scalar(select(AiJob).where(AiJob.id == job_id).with_for_update())
    if job is None:
        raise ValueError("job absent")
    job.event_sequence += 1
    event = AiJobEvent(job_id=job_id, sequence=job.event_sequence, kind=kind, data=data)
    session.add(event)
    session.flush()
    # Count bound applies even to long-running jobs; idempotency/reservation is untouched.
    session.execute(delete(AiJobEvent).where(AiJobEvent.job_id == job_id,
        AiJobEvent.sequence <= job.event_sequence - MAX_EVENTS))
    return event


def recent_events(session, job_id, *, after=0, through=None, floor=None):
    statement = select(AiJobEvent).where(AiJobEvent.job_id == job_id,
        AiJobEvent.sequence > max(after, floor or 0),
        AiJobEvent.sequence > (_event_floor(job_id) if floor is None else floor),
        AiJobEvent.created_at >= func.clock_timestamp() - timedelta(days=RETENTION_DAYS),
    )
    if through is not None:
        statement = statement.where(AiJobEvent.sequence <= through)
    return session.scalars(statement.order_by(AiJobEvent.sequence).limit(MAX_EVENTS)).all()


def latest_progress(session, job_id):
    # Polling needs a label, not the whole draft journal on every request.
    return session.scalar(select(AiJobEvent.data).where(AiJobEvent.job_id == job_id,
        AiJobEvent.sequence > _event_floor(job_id),
        AiJobEvent.kind == "progress",
        AiJobEvent.created_at >= func.clock_timestamp() - timedelta(days=RETENTION_DAYS),
    ).order_by(AiJobEvent.sequence.desc()).limit(1))


def latest_failure(session, job_id):
    """Read an old diagnostic without backfilling or rewriting task history."""
    value = session.scalar(select(AiJobEvent.data).where(AiJobEvent.job_id == job_id,
        AiJobEvent.sequence > _event_floor(job_id),
        AiJobEvent.kind == "diagnostic").order_by(AiJobEvent.sequence.desc()).limit(1))
    if isinstance(value, dict) and value.get("failure_type") in SAFE_FAILURE_TYPES:
        return value["failure_type"]
    return None


class EventWriter:
    """Batch actual model deltas; heartbeat never invents progress or text."""
    def __init__(self, database, job_id):
        self.database, self.job_id = database, job_id
        self.pending = ""
        self.last_flush = time.monotonic()
        self.total_chars = 0
        self._lock = RLock()
        self._closed = False

    def __call__(self, kind, data):
        with self._lock:
            if not self._closed:
                self._accept(kind, data)

    def _accept(self, kind, data):
        if kind == "delta":
            # Drafts are optional, bounded and never a successful final result.
            text = data["text"][:max(0, 4000 - self.total_chars)]
            self.total_chars += len(text)
            self.pending += text
            if len(self.pending) < 192 and time.monotonic() - self.last_flush < 0.2:
                return
            self.flush()
        else:
            self.flush()
            self._write(kind, data)

    def close(self):
        # A timed-out to_thread tool can still hold a copied observer context.
        # Its late completion must never append progress after a terminal event.
        with self._lock:
            self.flush()
            self._closed = True

    def flush(self):
        with self._lock:
            self._flush()

    def _flush(self):
        while self.pending:
            block, self.pending = self.pending[:512], self.pending[512:]
            self._write("delta", {"text": block})
        self.last_flush = time.monotonic()

    def _write(self, kind, data):
        try:
            with self.database.transaction() as session:
                append_event(session, self.job_id, kind, data)
        except SQLAlchemyError:
            # Observability outages don't retry business execution or leak values.
            logger.warning("job event persistence unavailable")
