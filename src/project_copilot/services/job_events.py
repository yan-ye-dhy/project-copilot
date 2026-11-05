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
from project_copilot.infrastructure.job_progress import LABELS

MAX_EVENTS = 512
RETENTION_DAYS = 7
logger = logging.getLogger(__name__)


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
        if set(data) != {"failure_type"} or data["failure_type"] not in {
            "model_error", "model_timeout", "total_timeout", "invalid_final", "knowledge_retrieval_failed",
            "tool_timeout", "model_budget", "tool_budget", "context_budget", "tool_protocol_error",
            "execution_error", "invalid_schema", "invalid_json", "invalid_citation", "authentication_error",
        }:
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


def recent_events(session, job_id, *, after=0):
    return session.scalars(select(AiJobEvent).where(AiJobEvent.job_id == job_id,
        AiJobEvent.sequence > after,
        AiJobEvent.created_at >= func.clock_timestamp() - timedelta(days=RETENTION_DAYS),
    ).order_by(AiJobEvent.sequence).limit(MAX_EVENTS)).all()


def latest_progress(session, job_id):
    # Polling needs a label, not the whole draft journal on every request.
    return session.scalar(select(AiJobEvent.data).where(AiJobEvent.job_id == job_id,
        AiJobEvent.kind == "progress",
        AiJobEvent.created_at >= func.clock_timestamp() - timedelta(days=RETENTION_DAYS),
    ).order_by(AiJobEvent.sequence.desc()).limit(1))


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
