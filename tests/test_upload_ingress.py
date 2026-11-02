"""Public upload admission against the isolated PostgreSQL test service."""
import asyncio

import pytest
from fastapi import HTTPException, Request, Response
from sqlalchemy import select

from project_copilot.api import jobs
from project_copilot.db.models import AiRequestReservation
from project_copilot.services.auth import csrf_token
from project_copilot.services.upload_ingress import UploadIngressBusy, acquire_upload_slot, release_upload_slot
from test_public_jobs import app
from test_public_upload_jobs import identities


def test_slot_is_global_and_released(ticket_database):
    slot = acquire_upload_slot(ticket_database)
    try:
        with pytest.raises(UploadIngressBusy):
            acquire_upload_slot(ticket_database)
    finally:
        release_upload_slot(slot)
    slot = acquire_upload_slot(ticket_database)
    release_upload_slot(slot)
    assert ticket_database.engine.pool.checkedout() == 0


def test_slow_stream_times_out_without_quota_and_releases_slot(ticket_database, monkeypatch):
    token = identities(ticket_database)["admin1"]
    application = app(ticket_database)
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in {
        "Origin": "https://demo.example", "X-CSRF-Token": csrf_token(token),
        "Idempotency-Key": "slow-upload-key", "Cookie": f"__Host-pc_session={token}",
        "Content-Type": "multipart/form-data; boundary=example",
    }.items()]
    scope = {"type": "http", "method": "POST", "path": "/jobs/upload", "headers": raw_headers,
             "scheme": "https", "server": ("demo.example", 443), "app": application,
             "query_string": b"", "http_version": "1.1"}
    async def receive():
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"", "more_body": True}
    monkeypatch.setattr(jobs, "_UPLOAD_READ_TIMEOUT_SECONDS", 0.05)
    async def run():
        with pytest.raises(HTTPException) as error:
            await jobs.submit_upload(Request(scope, receive), Response())
        assert error.value.status_code == 408
    asyncio.run(run())
    with ticket_database.transaction() as session:
        assert not list(session.scalars(select(AiRequestReservation)))
    slot = acquire_upload_slot(ticket_database)
    release_upload_slot(slot)


def test_cancelled_stream_releases_slot(ticket_database):
    token = identities(ticket_database)["admin1"]
    application = app(ticket_database)
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in {
        "Origin": "https://demo.example", "X-CSRF-Token": csrf_token(token),
        "Idempotency-Key": "cancel-upload-key", "Cookie": f"__Host-pc_session={token}",
        "Content-Type": "multipart/form-data; boundary=example",
    }.items()]
    scope = {"type": "http", "method": "POST", "path": "/jobs/upload", "headers": raw_headers,
             "scheme": "https", "server": ("demo.example", 443), "app": application,
             "query_string": b"", "http_version": "1.1"}
    async def run():
        entered = asyncio.Event()
        async def receive():
            entered.set()
            await asyncio.sleep(60)
            return {"type": "http.request", "body": b"", "more_body": True}
        task = asyncio.create_task(jobs.submit_upload(Request(scope, receive), Response()))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    slot = acquire_upload_slot(ticket_database)
    release_upload_slot(slot)
