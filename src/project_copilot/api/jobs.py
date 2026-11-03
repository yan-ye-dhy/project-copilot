"""Durable, owner-bound AI requests for the public demo."""
from hashlib import sha256
import asyncio
import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import object_session
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from project_copilot.api.access import public_identity, public_mode
from project_copilot.api.actions import credentials
from project_copilot.api.auth import _token_from_cookie
from project_copilot.api.documents import _validate_file
from project_copilot.db.jobs import AiJob, AiJobUpload
from project_copilot.db.models import AiRequestReservation
from project_copilot.db.operations import JobContentTombstone
from project_copilot.schemas.agent import AgentQuestion
from project_copilot.schemas.rag import RagQuestion
from project_copilot.services.approvals import ApprovalService, ContextRejected
from project_copilot.services.auth import AuthService, AuthenticationRejected
from project_copilot.services.ai_audit import record_attempt
from project_copilot.services.quotas import IdempotencyConflict, QuotaExceeded, QuotaService
from project_copilot.services.upload_ingress import UploadIngressBusy, acquire_upload_slot, release_upload_slot

router = APIRouter(prefix="/jobs", tags=["jobs"])
_UPLOAD_READ_TIMEOUT_SECONDS = 30


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["agent", "rag"]
    payload: dict


def _quota(session, settings):
    return QuotaService(session, user_daily_limit=settings.max_daily_requests_per_user,
                        global_daily_limit=settings.max_daily_requests_global,
                        user_concurrency_limit=settings.max_concurrent_requests_per_user)


def _response(job, reservation):
    session = object_session(job)
    result_expired = session is not None and session.get(JobContentTombstone, job.id) is not None
    return {"job_id": str(job.id), "kind": reservation.kind, "status": reservation.state,
            "result": job.result if reservation.state == "succeeded" and not result_expired else None,
            "result_expired": result_expired}


def _record_rejected(database, user_id, kind, outcome):
    """Commit rejected attempts separately after the request transaction rolls back."""
    if user_id is None:
        return
    try:
        with database.transaction() as session:
            record_attempt(session, user_id, kind, outcome)
    except (SQLAlchemyError, ValueError):
        # Never claim a recorded rejection if the durable audit write failed.
        raise HTTPException(503, "服务暂不可用") from None


def _upload_identity(request: Request):
    database = request.app.state.database
    if database is None:
        raise HTTPException(503, "服务暂不可用")
    with database.transaction() as session:
        principal = public_identity(request, session, write=True)
        if principal.role != "admin":
            raise HTTPException(403, "仅管理员可管理公共知识库")
        _, login = AuthService(session).authenticate(_token_from_cookie(request), for_update=True)
        return principal.id, login.id


def _reserve_upload(request, identity, key, content, source_name, media_type, content_sha256):
    database, settings = request.app.state.database, request.app.state.settings
    with database.transaction() as session:
        # The upload may have taken time; revalidate current role and session
        # while holding the locks that protect the reservation transaction.
        principal = public_identity(request, session, write=True)
        if principal.role != "admin":
            raise HTTPException(403, "仅管理员可管理公共知识库")
        _, login = AuthService(session).authenticate(_token_from_cookie(request), for_update=True)
        if (principal.id, login.id) != identity:
            raise HTTPException(403, "会话已变更")
        canonical = json.dumps({"kind": "upload", "source_name": source_name, "media_type": media_type,
                                "content_sha256": content_sha256, "session_id": str(login.id)},
                               ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = sha256(canonical.encode("utf-8")).hexdigest()
        reservation, reused = _quota(session, settings).reserve(principal.id, key, fingerprint, "upload")
        if reused:
            job = session.get(AiJob, reservation.id)
            if job is None:
                raise HTTPException(503, "任务状态不可用")
        else:
            job = AiJob(id=reservation.id, login_session_id=login.id, context_id=None,
                        payload={"source_name": source_name, "media_type": media_type,
                                 "content_sha256": content_sha256, "size_bytes": len(content)})
            session.add(job)
            session.flush()
            session.add(AiJobUpload(job_id=job.id, content=content, source_name=source_name,
                                    media_type=media_type, content_sha256=content_sha256))
            session.flush()
        record_attempt(session, principal.id, "upload", "reused" if reused else "accepted", reservation.id)
        return _response(job, reservation)


@router.post("/upload", status_code=202)
async def submit_upload(request: Request, response: Response):
    """Bounded multipart upload is stored with its quota reservation atomically."""
    settings = request.app.state.settings
    if not public_mode(settings):
        raise HTTPException(404, "接口不存在")
    keys = request.headers.getlist("idempotency-key")
    if len(keys) != 1:
        raise HTTPException(400, "请提供一次请求的幂等键")
    slot = None
    identity = None
    try:
        identity = await run_in_threadpool(_upload_identity, request)
        slot = await run_in_threadpool(acquire_upload_slot, request.app.state.database)
        content_types = request.headers.getlist("content-type")
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "multipart/form-data":
            raise HTTPException(415, "上传请求必须使用multipart/form-data")
        lengths = request.headers.getlist("content-length")
        limit = 4 * 1024 * 1024 + 64 * 1024
        if len(lengths) > 1 or (lengths and (not lengths[0].isascii() or not lengths[0].isdigit())):
            raise HTTPException(400, "请求长度无效")
        if lengths and int(lengths[0]) > limit:
            raise HTTPException(413, "上传请求超过允许的大小")
        body = bytearray()
        async with asyncio.timeout(_UPLOAD_READ_TIMEOUT_SECONDS):
            async for block in request.stream():
                if len(body) + len(block) > limit:
                    raise HTTPException(413, "上传请求超过允许的大小")
                body.extend(block)

        # Re-parse the bounded stream with Starlette's multipart parser; its
        # context manager closes spooled temporary files on every exit path.
        emitted = False
        async def receive():
            nonlocal emitted
            if emitted:
                return {"type": "http.disconnect"}
            emitted = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        parsed = Request(request.scope, receive=receive)
        async with parsed.form(max_files=1, max_fields=0, max_part_size=64 * 1024) as form:
            items = list(form.multi_items())
            if len(items) != 1 or items[0][0] != "file" or not isinstance(items[0][1], UploadFile):
                raise HTTPException(422, "请提供一个名为file的上传文件")
            upload = items[0][1]
            source_name, suffix = _validate_file(upload)
            # PDF parsing can expand far beyond its compressed file size. Until
            # parsing has process-level resource bounds, keep this path closed.
            if suffix == ".pdf":
                raise HTTPException(503, "公开 PDF 上传尚未完成资源安全验收")
            media_type = (upload.content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
            file_limit = min(settings.upload_max_bytes, 4 * 1024 * 1024)
            content = bytearray()
            while chunk := await upload.read(64 * 1024):
                if len(content) + len(chunk) > file_limit:
                    raise HTTPException(413, "上传文件超过允许的大小")
                content.extend(chunk)
            if not content:
                raise HTTPException(422, "上传文件不能为空")
            data = bytes(content)
        result = await run_in_threadpool(_reserve_upload, request, identity, keys[0], data, source_name,
                                         media_type, sha256(data).hexdigest())
    except HTTPException as exc:
        if identity is not None and exc.status_code not in (401, 403):
            await run_in_threadpool(_record_rejected, request.app.state.database, identity[0], "upload", "failed")
        raise
    except AuthenticationRejected:
        raise HTTPException(403, "会话不可用") from None
    except UploadIngressBusy:
        if identity is not None:
            await run_in_threadpool(_record_rejected, request.app.state.database, identity[0], "upload", "failed")
        raise HTTPException(429, "当前有文件正在接收，请稍后再试") from None
    except TimeoutError:
        if identity is not None:
            await run_in_threadpool(_record_rejected, request.app.state.database, identity[0], "upload", "failed")
        raise HTTPException(408, "上传读取超时，请检查网络") from None
    except IdempotencyConflict:
        if identity is not None:
            await run_in_threadpool(_record_rejected, request.app.state.database, identity[0], "upload", "failed")
        raise HTTPException(409, "幂等键已用于其他请求") from None
    except QuotaExceeded:
        if identity is not None:
            await run_in_threadpool(_record_rejected, request.app.state.database, identity[0], "upload", "quota_rejected")
        raise HTTPException(429, "当前请求已达限额，请稍后再试") from None
    except ValueError:
        if identity is not None:
            await run_in_threadpool(_record_rejected, request.app.state.database, identity[0], "upload", "failed")
        raise HTTPException(400, "请求格式无效") from None
    except SQLAlchemyError:
        if identity is not None:
            await run_in_threadpool(_record_rejected, request.app.state.database, identity[0], "upload", "failed")
        raise HTTPException(503, "服务暂不可用") from None
    finally:
        if slot is not None:
            # Release even if the client disconnects or the handler is cancelled.
            release_upload_slot(slot)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("", status_code=202)
def submit(body: JobInput, request: Request, response: Response):
    settings = request.app.state.settings
    if not public_mode(settings):
        raise HTTPException(404, "接口不存在")
    database = request.app.state.database
    if database is None:
        raise HTTPException(503, "服务暂不可用")
    keys = request.headers.getlist("idempotency-key")
    if len(keys) != 1:
        raise HTTPException(400, "请提供一次请求的幂等键")
    user_id = None
    try:
        context = credentials(request, optional=True)
        if body.kind != "agent" and context is not None:
            raise HTTPException(400, "此请求不支持上下文")
        with database.transaction() as session:
            principal = public_identity(request, session, write=True)
            user_id = principal.id
            payload = (AgentQuestion if body.kind == "agent" else RagQuestion).model_validate(body.payload).model_dump(mode="json")
            _, login = AuthService(session).authenticate(_token_from_cookie(request), for_update=True)
            context_id = None
            if context is not None:
                checked = ApprovalService(session, principal=principal, enforce_ownership=True).authenticate(*context)
                context_id = checked.id
            # Bind replay identity to context and session; never store bearer tokens.
            canonical = json.dumps({"kind": body.kind, "payload": payload, "context_id": str(context_id) if context_id else None,
                                    "session_id": str(login.id)}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fingerprint = sha256(canonical.encode("utf-8")).hexdigest()
            reservation, reused = _quota(session, settings).reserve(principal.id, keys[0], fingerprint, body.kind)
            if reused:
                job = session.get(AiJob, reservation.id)
                if job is None:
                    raise HTTPException(503, "任务状态不可用")
            else:
                job = AiJob(id=reservation.id, login_session_id=login.id, context_id=context_id, payload=payload)
                session.add(job)
                session.flush()
            record_attempt(session, principal.id, body.kind, "reused" if reused else "accepted", reservation.id)
            result = _response(job, reservation)
    except ValidationError:
        _record_rejected(database, user_id, body.kind, "failed")
        raise HTTPException(422, "请检查问题格式") from None
    except (ContextRejected, AuthenticationRejected):
        _record_rejected(database, user_id, body.kind, "failed")
        raise HTTPException(403, "会话或上下文不可用") from None
    except IdempotencyConflict:
        _record_rejected(database, user_id, body.kind, "failed")
        raise HTTPException(409, "幂等键已用于其他请求") from None
    except QuotaExceeded:
        _record_rejected(database, user_id, body.kind, "quota_rejected")
        raise HTTPException(429, "当前请求已达限额，请稍后再试") from None
    except ValueError:
        _record_rejected(database, user_id, body.kind, "failed")
        raise HTTPException(400, "请求格式无效") from None
    except SQLAlchemyError:
        _record_rejected(database, user_id, body.kind, "failed")
        raise HTTPException(503, "服务暂不可用") from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/{job_id}")
def status(job_id: UUID, request: Request, response: Response):
    settings = request.app.state.settings
    if not public_mode(settings):
        raise HTTPException(404, "接口不存在")
    database = request.app.state.database
    if database is None:
        raise HTTPException(503, "服务暂不可用")
    try:
        with database.transaction() as session:
            principal = public_identity(request, session)
            stmt = select(AiJob, AiRequestReservation).join(AiRequestReservation).where(AiJob.id == job_id)
            if principal.role != "admin":
                stmt = stmt.where(AiRequestReservation.user_id == principal.id)
            row = session.execute(stmt).one_or_none()
            if row is None:
                raise HTTPException(404, "任务不存在")
            result = _response(*row)
    except SQLAlchemyError:
        raise HTTPException(503, "服务暂不可用") from None
    response.headers["Cache-Control"] = "no-store"
    return result
