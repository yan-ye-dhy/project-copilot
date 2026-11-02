"""Execute durable AI jobs at most once; uncertain outcomes remain reserved."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
from uuid import UUID

from sqlalchemy import func, select
from starlette.datastructures import Headers, UploadFile

from project_copilot.agent.service import create_agent_service
from project_copilot.db.jobs import AiJob, AiJobUpload
from project_copilot.db.models import AiRequestReservation
from project_copilot.infrastructure.call_accounting import account_job
from project_copilot.rag.answering import create_answer_service
from project_copilot.schemas.agent import AgentQuestion
from project_copilot.schemas.rag import RagQuestion
from project_copilot.services.auth import AuthService, AuthenticationRejected
from project_copilot.services.quotas import QuotaService


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    user_id: UUID
    session_id: UUID
    context_id: UUID | None
    kind: str
    payload: dict
    attachment: bytes | None = None
    source_name: str | None = None
    media_type: str | None = None
    content_sha256: str | None = None


def expire_unclaimed(database, *, ttl_seconds: int, batch_size: int = 100) -> int:
    """Expire only never-claimed jobs, deleting any stored upload bytes.

    A running/unknown job may still have a live thread or upstream request and
    is deliberately never released by a clock-based cleanup.
    """
    if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 30 * 86400:
        raise ValueError("invalid queue lifetime")
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError("invalid cleanup batch")
    with database.transaction() as session:
        now = session.scalar(select(func.clock_timestamp()))
        rows = session.scalars(select(AiRequestReservation).where(
            AiRequestReservation.state == "reserved", AiRequestReservation.created_at < now - timedelta(seconds=ttl_seconds),
        ).order_by(AiRequestReservation.created_at, AiRequestReservation.id)
            .with_for_update(skip_locked=True).limit(batch_size)).all()
        for reservation in rows:
            attachment = session.get(AiJobUpload, reservation.id)
            if attachment is not None:
                session.delete(attachment)
            job = session.get(AiJob, reservation.id)
            if job is not None:
                job.payload = {}
            reservation.state, reservation.finished_at = "failed", now
        return len(rows)


def claim_one(database, *, ttl_seconds: int | None = None) -> ClaimedJob | None:
    # One transaction binds the job payload to the reservation claim.
    with database.transaction() as session:
        statement = select(AiJob, AiRequestReservation).join(AiRequestReservation).where(AiRequestReservation.state == "reserved")
        if ttl_seconds is not None:
            if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 30 * 86400:
                raise ValueError("invalid queue lifetime")
            statement = statement.where(AiRequestReservation.created_at >= func.clock_timestamp() - timedelta(seconds=ttl_seconds))
        row = session.execute(
            statement
            .order_by(AiRequestReservation.created_at, AiRequestReservation.id)
            .with_for_update(of=AiRequestReservation, skip_locked=True).limit(1)
        ).one_or_none()
        if row is None:
            return None
        job, reservation = row
        if not QuotaService(session).claim(reservation.user_id, reservation.id):
            return None
        attachment = session.get(AiJobUpload, job.id) if reservation.kind == "upload" else None
        result = ClaimedJob(job.id, reservation.user_id, job.login_session_id,
                            job.context_id, reservation.kind, dict(job.payload),
                            bytes(attachment.content) if attachment else None,
                            attachment.source_name if attachment else None,
                            attachment.media_type if attachment else None,
                            attachment.content_sha256 if attachment else None)
        # Once claimed, the bytes are not retained in PostgreSQL or replayed after a crash.
        if attachment is not None:
            session.delete(attachment)
        return result


async def _agent(settings, database, job):
    context = (job.context_id, None) if job.context_id else None
    async with create_agent_service(settings, database, context=context, auth_session_id=job.session_id) as service:
        return await service.run(AgentQuestion.model_validate(job.payload), run_id=str(job.id))


def execute(settings, database, job):
    # Revalidate before any model call; internal session id is never accepted from HTTP.
    with database.transaction() as session:
        principal, _ = AuthService(session).authenticate_session_id(job.session_id)
        if principal.id != job.user_id:
            raise AuthenticationRejected()
    if job.kind == "agent":
        return asyncio.run(_agent(settings, database, job))
    if job.kind == "upload":
        from project_copilot.api.documents import _ingest_upload
        if (job.attachment is None or job.source_name is None or job.media_type is None or
                sha256(job.attachment).hexdigest() != job.content_sha256):
            raise ValueError("upload attachment is unavailable")
        # Keep user/session stable through indexing; accounting uses read-only
        # session checks in another connection to avoid acquiring the same lock.
        with database.transaction() as session:
            principal, _ = AuthService(session).authenticate_session_id(job.session_id, for_update=True)
            if principal.id != job.user_id or principal.role != "admin":
                raise AuthenticationRejected()
            upload = UploadFile(file=BytesIO(job.attachment), filename=job.source_name,
                                headers=Headers({"content-type": job.media_type}))
            with account_job(database, job.user_id, job.id, max_attempts=settings.max_provider_attempts_per_job,
                             prelocked_admin=True):
                return _ingest_upload(upload, settings, str(job.id))
    with create_answer_service(settings) as service:
        return service.answer(RagQuestion.model_validate(job.payload).question)


def process_one(database, settings, *, runner=execute) -> bool:
    job = claim_one(database, ttl_seconds=settings.public_job_queue_ttl_seconds)
    if job is None:
        return False
    try:
        with account_job(database, job.user_id, job.id, max_attempts=settings.max_provider_attempts_per_job):
            result = runner(settings, database, job)
        serialized = result.model_dump(mode="json")
        # A timed-out tool may still be running in a thread; do not free its slot.
        if job.kind == "agent" and (result.status in {"failed", "budget_exhausted"} or
                                    any("timeout" in (event.failure_type or "") for event in result.events) or
                                    any("timeout" in (event.get("failure_type") or "") for event in result.model_events)):
            outcome, usage, serialized = "unknown", result.usage, None
        else:
            outcome = "succeeded"
            usage = (result.usage if job.kind == "agent" else
                     (result.generation.usage if result.generation else None) if job.kind == "rag" else None)
        if usage is not None:
            allowed = {"prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens",
                       "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
            usage = {key: value for key, value in usage.items()
                     if key in allowed and type(value) is int and 0 <= value <= 2**63 - 1}
    except Exception:
        # Even authentication can fail after a model call, during a later tool step.
        # The provider may have processed a request when its response is lost.
        outcome, usage, serialized = "unknown", None, None
    with database.transaction() as session:
        QuotaService(session).finish(job.user_id, job.id, outcome=outcome, usage=usage)
        if serialized is not None:
            stored = session.get(AiJob, job.id)
            stored.result = serialized
    return True
