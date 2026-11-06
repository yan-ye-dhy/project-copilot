"""Execute jobs at most once; personal slots end only with execution proof."""
import asyncio
from dataclasses import dataclass
from contextlib import nullcontext
import logging
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
from project_copilot.infrastructure.job_progress import observe, progress
from project_copilot.infrastructure.execution_tracking import ExecutionTracker, track_execution, current_execution
from project_copilot.services.execution_slots import (ExecutionLease, register_execution, reclaim_ended,
    mark_local_done_and_release, finish_personal_execution)
from project_copilot.services.personal_profile import personal_runtime_settings
from project_copilot.services.job_failures import safe_failure
from project_copilot.infrastructure.tracing import create_trace_runtime, current_trace_id
from project_copilot.services.job_events import EventWriter, append_event
from project_copilot.rag.answering import create_answer_service
from project_copilot.schemas.agent import AgentQuestion
from project_copilot.schemas.rag import RagQuestion
from project_copilot.services.auth import AuthService, AuthenticationRejected
from project_copilot.services.quotas import QuotaService
from project_copilot.services.release_control import ReleaseClosed, dependencies_ready, execution_lease, require_open


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
    execution_lease: object | None = None


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
            reservation.slot_released_at = now
        return len(rows)


def claim_one(database, *, ttl_seconds: int | None = None, settings=None) -> ClaimedJob | None:
    leases = []
    try:
        return _claim_one(database, ttl_seconds=ttl_seconds, settings=settings, leases=leases)
    except BaseException:
        # No execution can start before this claim transaction commits.
        for lease in leases:
            lease.close()
        raise


def _claim_one(database, *, ttl_seconds, settings, leases):
    # One transaction binds the job payload, physical lease and unique execution token.
    with database.transaction() as session:
        statement = select(AiJob, AiRequestReservation).join(AiRequestReservation).where(AiRequestReservation.state == "reserved")
        release = require_open(session, settings, worker=True) if settings is not None else None
        if release is not None and release.state == "PERSONAL_DEMO":
            # An old grant's queued task remains evidence, never an implicit
            # retry. Filtering also prevents it blocking the current grant.
            reclaim_ended(session, settings, user_id=UUID(release.allowed_user_ids[0]))
            statement = statement.where(
                AiRequestReservation.personal_demo_grant_id == UUID(release.evidence["grant_id"]),
                AiRequestReservation.user_id == UUID(release.allowed_user_ids[0]),
            )
        else:
            statement = statement.where(AiRequestReservation.personal_demo_grant_id.is_(None))
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
        if settings is not None:
            require_open(session, settings, user_id=reservation.user_id, worker=True, reservation=reservation)
        if not QuotaService(session).claim(reservation.user_id, reservation.id):
            return None
        attachment = session.get(AiJobUpload, job.id) if reservation.kind == "upload" else None
        lease = None
        if reservation.personal_demo_grant_id is not None:
            lease = ExecutionLease.create(settings.manifest_dir, job.id)
            leases.append(lease)
            register_execution(session, reservation, lease)
        result = ClaimedJob(job.id, reservation.user_id, job.login_session_id,
                            job.context_id, reservation.kind, dict(job.payload),
                            bytes(attachment.content) if attachment else None,
                            attachment.source_name if attachment else None,
                            attachment.media_type if attachment else None,
                            attachment.content_sha256 if attachment else None, lease)
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
    # Keep the existing database execution lease through physical work drain.
    # PERSONAL_DEMO additionally has a durable per-job OS lease for crash proof.
    with execution_lease(database):
        try:
            with database.transaction() as session:
                require_open(session, settings, worker=True)
            dependencies_ready(settings)
            return _process_one(database, settings, runner=runner)
        except ReleaseClosed:
            return False


def _process_one(database, settings, *, runner):
    job = claim_one(database, ttl_seconds=settings.public_job_queue_ttl_seconds, settings=settings)
    if job is None:
        return False
    lease = job.execution_lease
    if lease is None:
        return _run_traced(database, settings, job, runner=runner)
    tracker = ExecutionTracker(database, job.id, lease.token)
    drained = False
    try:
        with track_execution(tracker):
            try:
                return _run_traced(database, settings, job, runner=runner)
            finally:
                # Awaiting a tool can time out before its real thread/future ends.
                # Keep BOTH execution leases held until physical work has drained.
                tracker.drain()
                drained = True
                mark_local_done_and_release(database, settings, job.id, lease.token, lease)
    finally:
        if drained:
            lease.close()
        else:
            # If drain itself is interrupted, don't turn a lost Python reference
            # into false death proof. The OS releases this only at process exit.
            _INCOMPLETE_LEASES.append(lease)


_INCOMPLETE_LEASES = []


def _run_traced(database, settings, job, *, runner):
    runtime = create_trace_runtime(settings)
    trace_id = None
    try:
        with runtime.activate(str(job.id), name="worker.job") if runtime else nullcontext():
            trace_id = current_trace_id() if runtime else None
            return _execute_claimed(database, settings, job, runner=runner)
    finally:
        if runtime is not None:
            try:
                result = runtime.export(trace_id) if trace_id else None
                logger = logging.getLogger(__name__)
                log = logger.info if result and result.exported else logger.warning
                log("worker_trace job_id=%s trace_id=%s exported=%s reason=%s", job.id,
                    trace_id or "none", str(bool(result and result.exported)).lower(),
                    result.reason if result else "no_trace")
            except Exception as exc:
                logging.getLogger(__name__).warning("worker_trace_export_failed job_id=%s error_type=%s",
                    job.id, type(exc).__name__)
            finally:
                try:
                    runtime.close()
                except Exception as exc:
                    logging.getLogger(__name__).warning("worker_trace_close_failed job_id=%s error_type=%s",
                        job.id, type(exc).__name__)


def _execute_claimed(database, settings, job, *, runner):
    writer = EventWriter(database, job.id)
    failure = None
    personal = job.execution_lease is not None
    runtime_settings = personal_runtime_settings(settings) if personal else settings
    try:
        with observe(writer), account_job(database, job.user_id, job.id,
                max_attempts=runtime_settings.max_provider_attempts_per_job, settings=settings):
            progress("running")
            result = runner(runtime_settings, database, job)
        serialized = result.model_dump(mode="json")
        # A timed-out tool may still be running in a thread; do not free its slot.
        if job.kind == "agent" and (result.status in {"failed", "budget_exhausted"} or
                                    (not personal and (any("timeout" in (event.failure_type or "") for event in result.events) or
                                    any("timeout" in (event.get("failure_type") or "") for event in result.model_events)))):
            outcome, usage, serialized = "failed" if personal else "unknown", result.usage, None
            failure = getattr(result, "failure_type", None) or "execution_error"
        else:
            outcome = "succeeded"
            usage = (result.usage if job.kind == "agent" else
                     (result.generation.usage if result.generation else None) if job.kind == "rag" else None)
        if usage is not None:
            allowed = {"prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens",
                       "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
            usage = {key: value for key, value in usage.items()
                     if key in allowed and type(value) is int and 0 <= value <= 2**63 - 1}
    except Exception as exc:
        # Never persist/log raw provider exceptions, prompts, tool payloads or credentials.
        from project_copilot.rag.answering import AnswerResponseError
        outcome, usage, serialized = "unknown", None, None
        failure = exc.reason if isinstance(exc, AnswerResponseError) else "execution_error"
        from project_copilot.services.quotas import QuotaExceeded
        if isinstance(exc, QuotaExceeded) and exc.reason == "provider_attempts":
            failure = "provider_budget"
        if personal and (isinstance(exc, AnswerResponseError) or failure == "provider_budget"):
            outcome = "failed"
    writer.flush()
    if failure is not None:
        failure = safe_failure(failure)
        if personal and failure == "execution_error":
            outcome = "unknown"
        writer("diagnostic", {"failure_type": failure})
        logging.getLogger(__name__).warning("worker_outcome job_id=%s failure_type=%s", job.id, failure)
    if personal:
        # Preserve known usage/diagnostics without publishing a premature terminal
        # state. A crash here remains running/unknown and is never replayed.
        with database.transaction() as session:
            stored = session.get(AiJob, job.id)
            reservation = session.get(AiRequestReservation, job.id, with_for_update=True)
            reservation.usage = usage
            stored.failure_type = failure
        tracker = current_execution()
        if tracker is None:
            raise RuntimeError("personal execution tracker unavailable")
        with observe(writer):
            progress("draining" if tracker.pending else "finalizing")
        tracker.drain()
    writer.close()
    with database.transaction() as session:
        if personal:
            outcome = finish_personal_execution(session, job, job.execution_lease, outcome=outcome, usage=usage)
        else:
            QuotaService(session).finish(job.user_id, job.id, outcome=outcome, usage=usage)
        stored = session.get(AiJob, job.id)
        stored.failure_type = failure
        stored.result = serialized if outcome == "succeeded" else None
        append_event(session, job.id, "terminal", {"status": outcome})
    return True
