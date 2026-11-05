"""Authenticated read-only SSE; disconnect never cancels or resubmits work."""
import asyncio
import json
import time
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from project_copilot.api.access import public_identity, public_mode
from project_copilot.db.jobs import AiJob
from project_copilot.db.models import AiRequestReservation
from project_copilot.db.operations import JobContentTombstone
from project_copilot.services.job_events import recent_events
from project_copilot.services.release_control import ReleaseClosed, require_open

STREAM_SECONDS = 20
POLL_SECONDS = 1
HEARTBEAT_SECONDS = 5
TERMINAL = {"succeeded", "failed", "unknown"}


def cursor(request):
    values = request.query_params.getlist("after")
    headers = request.headers.getlist("last-event-id")
    if set(request.query_params) - {"after"} or len(values) > 1 or len(headers) > 1:
        raise HTTPException(400, "事件序号无效")
    for value in [*values, *headers]:
        if not value or not value.isascii() or not value.isdigit() or len(value) > 10 or int(value) > 2147483647:
            raise HTTPException(400, "事件序号无效")
    return max([0, *(int(value) for value in [*values, *headers])])


def read_batch(request, job_id, after):
    try:
        with request.app.state.database.transaction() as session:
            principal = public_identity(request, session)
            require_open(session, request.app.state.settings, user_id=principal.id)
            row = session.execute(select(AiJob, AiRequestReservation).join(AiRequestReservation).where(
                AiJob.id == job_id, AiRequestReservation.user_id == principal.id)).one_or_none()
            if row is None:
                raise HTTPException(404, "任务不存在")
            job, reservation = row
            expired = session.get(JobContentTombstone, job_id) is not None
            events = [] if expired else recent_events(session, job_id, after=after)
            return {"status": reservation.state, "cursor": job.event_sequence,
                    "expired": expired, "events": [
                        {"id": event.sequence, "kind": event.kind, "data": event.data} for event in events]}
    except ReleaseClosed:
        raise HTTPException(503, "个人体验或发布授权已关闭") from None
    except SQLAlchemyError:
        raise HTTPException(503, "任务事件暂不可用，请查询任务状态") from None


def encode(kind, data, sequence=None):
    identity = "" if sequence is None else f"id: {sequence}\n"
    return f"{identity}event: {kind}\ndata: {json.dumps(data, ensure_ascii=True, separators=(',', ':'))}\n\n"


async def stream_events(request: Request, job_id: UUID):
    if not public_mode(request.app.state.settings):
        raise HTTPException(404, "接口不存在")
    if request.app.state.database is None:
        raise HTTPException(503, "服务暂不可用")
    after = cursor(request)
    # Authenticate/authorize before sending an HTTP200 or an event byte.
    initial = await run_in_threadpool(read_batch, request, job_id, after)

    async def generate():
        nonlocal after
        deadline, heartbeat = time.monotonic() + STREAM_SECONDS, time.monotonic()
        batch = initial
        yield ": connected\n\n"
        while time.monotonic() < deadline:
            if await request.is_disconnected():
                return
            events = batch["events"]
            # Missing retained history (or a tampered future cursor) is explicit.
            # A reset discards provisional text; the canonical GET remains authoritative.
            if batch["expired"] or after > batch["cursor"] or (events and events[0]["id"] > after + 1) or (not events and after < batch["cursor"]):
                after = batch["cursor"]
                yield encode("reset", {"status": batch["status"], "cursor_reset": True}, after)
            else:
                for event in events:
                    after = event["id"]
                    yield encode(event["kind"], event["data"], after)
            if batch["status"] in TERMINAL:
                # Old pre-journal jobs still terminate correctly; no synthetic model delta.
                yield encode("snapshot", {"status": batch["status"]})
                return
            if time.monotonic() - heartbeat >= HEARTBEAT_SECONDS:
                yield ": heartbeat\n\n"
                heartbeat = time.monotonic()
            await asyncio.sleep(POLL_SECONDS)
            try:
                batch = await run_in_threadpool(read_batch, request, job_id, after)
            except HTTPException:
                # Session/release expiry or DB outage: close, don't retain access.
                return
        yield encode("reconnect", {"after": after})

    return StreamingResponse(generate(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no",
        "X-Content-Type-Options": "nosniff",
    })
