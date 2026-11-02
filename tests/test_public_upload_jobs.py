"""Public upload reservation tests: isolated PostgreSQL, no provider calls."""
import asyncio
from hashlib import sha256
from uuid import UUID

import pytest
from fastapi import HTTPException, Request, Response
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from project_copilot.api import jobs as jobs_api
from project_copilot.api.jobs import submit_upload
from project_copilot.db.jobs import AiJob, AiJobUpload
from project_copilot.db.models import AiRequestReservation
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.services.auth import AuthService, csrf_token
from test_public_jobs import app, headers


PASSWORD = "test-password-12345"


def identities(database):
    with database.transaction() as session:
        service = AuthService(session)
        service.bootstrap_admin(RegisterInput(username="admin1", password=PASSWORD))
        service.register(RegisterInput(username="user1", password=PASSWORD), open_registration=True)
        return {name: service.login(LoginInput(username=name, password=PASSWORD)).token
                for name in ("admin1", "user1")}


def test_admin_upload_reserves_atomically_and_replay_is_bound(ticket_database):
    tokens = identities(ticket_database)
    application = app(ticket_database)
    with TestClient(application) as client:
        cookie = {"__Host-pc_session": tokens["admin1"]}
        auth = headers(tokens["admin1"], "upload-idempotency-1")
        send = lambda filename, content, mime: client.post("/jobs/upload", cookies=cookie, headers=auth,
                                                          files={"file": (filename, content, mime)})
        first = send("研发.txt", b"test content", "text/plain")
        assert first.status_code == 202 and first.json()["kind"] == "upload"
        job_id = UUID(first.json()["job_id"])
        assert send("研发.txt", b"test content", "text/plain").json()["job_id"] == str(job_id)
        assert send("研发.txt", b"changed content", "text/plain").status_code == 409
        assert send("other.txt", b"test content", "text/plain").status_code == 409
        assert send("研发.txt", b"test content", "application/octet-stream").status_code == 409
        assert client.post("/jobs/upload", cookies={"__Host-pc_session": tokens["user1"]},
                           headers=headers(tokens["user1"], "user-upload-key"),
                           files={"file": ("a.txt", b"text", "text/plain")}).status_code == 403
        assert client.get(f"/jobs/{job_id}", cookies={"__Host-pc_session": tokens["user1"]}).status_code == 404
        assert client.post("/jobs/upload", headers=auth, files={"file": ("a.txt", b"text", "text/plain")}).status_code == 401
    with ticket_database.transaction() as session:
        upload = session.get(AiJobUpload, job_id)
        job = session.get(AiJob, job_id)
        reservation = session.get(AiRequestReservation, job_id)
        assert upload.content == b"test content" and upload.content_sha256 == sha256(upload.content).hexdigest()
        assert upload.source_name == "研发.txt" and upload.media_type == "text/plain"
        assert job.payload["content_sha256"] == upload.content_sha256
        assert tokens["admin1"] not in str(job.payload)
        assert reservation.kind == "upload" and reservation.provider_attempts == 0
        assert len(list(session.scalars(select(AiJobUpload)))) == 1


def test_invalid_uploads_do_not_reserve_or_store_content(ticket_database):
    tokens = identities(ticket_database)
    application = app(ticket_database)
    application.state.settings = application.state.settings.model_copy(update={"upload_max_bytes": 128})
    cookie = {"__Host-pc_session": tokens["admin1"]}
    auth = headers(tokens["admin1"], "upload-bad-key")
    with TestClient(application) as client:
        send = lambda files: client.post("/jobs/upload", cookies=cookie, headers=auth, files=files)
        assert send({"file": ("test.txt", b"", "text/plain")}).status_code == 422
        assert send({"file": ("test.txt", b"a" * 129, "text/plain")}).status_code == 413
        assert send({"file": ("test.exe", b"x", "application/octet-stream")}).status_code == 415
        assert send({"file": ("test.pdf", b"x", "text/plain")}).status_code == 415
        assert send({"file": ("test.pdf", b"%PDF-1.4", "application/pdf")}).status_code == 503
        assert send({"file": ("test.pdf", b"%PDF-1.4 test", "application/pdf")}).status_code == 503
        assert send({"other": ("test.txt", b"x", "text/plain")}).status_code == 422
        assert send([("file", ("one.txt", b"a", "text/plain")), ("file", ("two.txt", b"b", "text/plain"))]).status_code == 400
        assert client.post("/jobs/upload", cookies=cookie, headers={**auth, "Origin": "https://attacker.test"},
                           files={"file": ("test.txt", b"x", "text/plain")}).status_code == 403
    with ticket_database.transaction() as session:
        assert not list(session.scalars(select(AiJobUpload)))
        assert not list(session.scalars(select(AiRequestReservation)))


def test_missing_content_length_still_limits_actual_stream(ticket_database):
    tokens = identities(ticket_database)
    application = app(ticket_database)
    token = tokens["admin1"]
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in {
        **headers(token, "stream-size-test"), "Cookie": f"__Host-pc_session={token}",
        "Content-Type": "multipart/form-data; boundary=example",
    }.items()]
    scope = {"type": "http", "method": "POST", "path": "/jobs/upload", "headers": raw_headers,
             "scheme": "https", "server": ("demo.example", 443), "app": application,
             "query_string": b"", "http_version": "1.1"}
    parts = iter([b"a" * (4 * 1024 * 1024), b"b" * (64 * 1024 + 1)])
    async def receive():
        return {"type": "http.request", "body": next(parts), "more_body": True}
    async def run():
        with pytest.raises(HTTPException) as error:
            await submit_upload(Request(scope, receive), Response())
        assert error.value.status_code == 413
    asyncio.run(run())
    with ticket_database.transaction() as session:
        assert not list(session.scalars(select(AiRequestReservation)))


def test_revocation_during_upload_prevents_reservation(ticket_database):
    tokens = identities(ticket_database)
    application = app(ticket_database)
    token = tokens["admin1"]
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in {
        **headers(token, "stream-revocation"), "Cookie": f"__Host-pc_session={token}",
        "Content-Type": "multipart/form-data; boundary=example",
    }.items()]
    scope = {"type": "http", "method": "POST", "path": "/jobs/upload", "headers": raw_headers,
             "scheme": "https", "server": ("demo.example", 443), "app": application,
             "query_string": b"", "http_version": "1.1"}
    raw = (b'--example\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n'
           b'Content-Type: text/plain\r\n\r\nhello\r\n--example--\r\n')
    async def receive():
        with ticket_database.transaction() as session:
            session.execute(text("UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE token_hash=:digest"),
                            {"digest": sha256(token.encode()).hexdigest()})
        return {"type": "http.request", "body": raw, "more_body": False}
    async def run():
        with pytest.raises(HTTPException) as error:
            await submit_upload(Request(scope, receive), Response())
        assert error.value.status_code == 401
    asyncio.run(run())
    with ticket_database.transaction() as session:
        assert not list(session.scalars(select(AiRequestReservation)))
        assert not list(session.scalars(select(AiJobUpload)))


def test_upload_storage_failure_rolls_back_job_and_quota(ticket_database, monkeypatch):
    tokens = identities(ticket_database)
    application = app(ticket_database)
    original = AiJobUpload

    def invalid_upload(**kwargs):
        kwargs["content"] = b""  # Deliberately violate the DB size constraint.
        return original(**kwargs)

    monkeypatch.setattr(jobs_api, "AiJobUpload", invalid_upload)
    with TestClient(application) as client:
        response = client.post("/jobs/upload", cookies={"__Host-pc_session": tokens["admin1"]},
                               headers=headers(tokens["admin1"], "storage-failure-key"),
                               files={"file": ("test.txt", b"valid", "text/plain")})
        assert response.status_code == 503
    with ticket_database.transaction() as session:
        assert not list(session.scalars(select(AiRequestReservation)))
        assert not list(session.scalars(select(AiJob)))
        assert not list(session.scalars(select(AiJobUpload)))
