"""Durable job behavior against isolated real PostgreSQL; no provider calls."""
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, text

from project_copilot.api.jobs import router
from project_copilot.config import Settings
from project_copilot.db.jobs import AiJob, AiJobUpload
from project_copilot.db.models import AiRequestReservation
from project_copilot.infrastructure.call_accounting import record_provider_attempt
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.services.ai_worker import claim_one, expire_unclaimed, process_one
from project_copilot.services.auth import AuthService, csrf_token
from project_copilot.services.quotas import QuotaService


def prepare(database):
    with database.transaction() as session:
        service = AuthService(session)
        for name in ("alice", "bob"):
            service.register(RegisterInput(username=name, password="test-password-12345"), open_registration=True)
        return {name: service.login(LoginInput(username=name, password="test-password-12345")).token
                for name in ("alice", "bob")}


def app(database):
    app = FastAPI()
    app.state.database = database
    app.state.settings = Settings(_env_file=None, public_demo=True, public_proxy_secret="a" * 32,
                                  auth_public_origin="https://demo.example")
    app.include_router(router)
    return app


def headers(token, key):
    return {"Origin": "https://demo.example", "X-CSRF-Token": csrf_token(token), "Idempotency-Key": key}


def test_reservation_job_commit_replay_owner_and_completion(ticket_database):
    tokens = prepare(ticket_database)
    application = app(ticket_database)
    calls = []

    def controlled_runner(settings, db, job):
        calls.append(job.id)
        record_provider_attempt()
        record_provider_attempt()
        return SimpleNamespace(model_dump=lambda **_: {"status": "answered", "answer": "测试答案"}, generation=None)

    with TestClient(application) as client:
        cookies = {name: {"__Host-pc_session": token} for name, token in tokens.items()}
        data = {"kind": "rag", "payload": {"question": "测试问题"}}
        submit = lambda body: client.post("/jobs", cookies=cookies["alice"], headers=headers(tokens["alice"], "same-key-123"), json=body)
        first = submit(data)
        assert first.status_code == 202
        job_id = first.json()["job_id"]
        assert submit(data).json()["job_id"] == job_id
        assert submit({"kind": "rag", "payload": {"question": "不同问题"}}).status_code == 409
        assert client.get("/jobs/" + job_id, cookies=cookies["bob"]).status_code == 404
        second = client.post("/jobs", cookies=cookies["alice"], headers=headers(tokens["alice"], "other-key-123"), json=data)
        assert second.status_code == 429
        assert process_one(ticket_database, application.state.settings, runner=controlled_runner)
        assert not process_one(ticket_database, application.state.settings, runner=controlled_runner)
        result = client.get("/jobs/" + job_id, cookies=cookies["alice"])
        assert result.status_code == 200 and result.json()["result"]["answer"] == "测试答案"
        assert calls == [UUID(job_id)]
    with ticket_database.transaction() as session:
        job = session.get(AiJob, UUID(job_id))
        reservation = session.get(AiRequestReservation, UUID(job_id))
        assert reservation.state == "succeeded" and reservation.provider_attempts == 2
        assert tokens["alice"] not in str(job.payload)
        assert session.scalar(select(AiRequestReservation.cost_status).where(AiRequestReservation.id == job.id)) == "unknown"


def test_uncertain_failure_does_not_requeue_or_release_user_slot(ticket_database):
    tokens = prepare(ticket_database)
    application = app(ticket_database)
    with TestClient(application) as client:
        cookie = {"__Host-pc_session": tokens["alice"]}
        payload = {"kind": "rag", "payload": {"question": "测试"}}
        initial = client.post("/jobs", cookies=cookie, headers=headers(tokens["alice"], "first-key-123"), json=payload)
        job_id = initial.json()["job_id"]
        def failed_runner(*_):
            raise RuntimeError("upstream status unknown")
        assert process_one(ticket_database, application.state.settings, runner=failed_runner)
        assert not process_one(ticket_database, application.state.settings, runner=failed_runner)
        assert client.get("/jobs/" + job_id, cookies=cookie).json()["status"] == "unknown"
        assert client.post("/jobs", cookies=cookie, headers=headers(tokens["alice"], "second-key-123"), json=payload).status_code == 429


def test_process_interruption_leaves_running_job_unreplayed(ticket_database):
    tokens = prepare(ticket_database)
    application = app(ticket_database)
    with TestClient(application) as client:
        cookie = {"__Host-pc_session": tokens["alice"]}
        payload = {"kind": "rag", "payload": {"question": "测试中断"}}
        initial = client.post("/jobs", cookies=cookie, headers=headers(tokens["alice"], "interrupt-key-123"), json=payload)
        job_id = initial.json()["job_id"]
        def interrupted(*_):
            raise KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            process_one(ticket_database, application.state.settings, runner=interrupted)
        assert not process_one(ticket_database, application.state.settings, runner=interrupted)
        assert client.get("/jobs/" + job_id, cookies=cookie).json()["status"] == "running"
        assert client.post("/jobs", cookies=cookie, headers=headers(tokens["alice"], "next-key-123"), json=payload).status_code == 429


def test_agent_tool_timeout_remains_unknown_even_if_an_answer_exists(ticket_database):
    tokens = prepare(ticket_database)
    application = app(ticket_database)
    with TestClient(application) as client:
        cookie = {"__Host-pc_session": tokens["alice"]}
        initial = client.post("/jobs", cookies=cookie, headers=headers(tokens["alice"], "tool-timeout-123"),
                              json={"kind": "agent", "payload": {"question": "查询"}})
        def timed_out(*_):
            return SimpleNamespace(status="answered", events=[SimpleNamespace(failure_type="tool_timeout")],
                                   model_events=[], usage=None, model_dump=lambda **__: {"answer": "不应返回"})
        assert process_one(ticket_database, application.state.settings, runner=timed_out)
        result = client.get("/jobs/" + initial.json()["job_id"], cookies=cookie).json()
        assert result["status"] == "unknown" and result["result"] is None


def test_upload_worker_revalidates_admin_and_accounts_before_mocked_provider(ticket_database, monkeypatch):
    from hashlib import sha256
    from project_copilot.api import documents
    db = ticket_database
    password = "test-admin-password-123"
    with db.transaction() as session:
        auth = AuthService(session)
        admin = auth.bootstrap_admin(RegisterInput(username="admin", password=password))
        issued = auth.login(LoginInput(username="admin", password=password))
        _, login = auth.authenticate(issued.token)
        reservation, _ = QuotaService(session).reserve(admin.id, "upload-key-123", "a" * 64, "upload")
        session.add(AiJob(id=reservation.id, login_session_id=login.id, payload={}))
        session.flush()
        content = b"synthetic test document"
        session.add(AiJobUpload(job_id=reservation.id, content=content, source_name="test.txt", media_type="text/plain",
                                content_sha256=sha256(content).hexdigest()))
        job_id = reservation.id
    observed = []
    def ingest(upload, settings, run_id):
        observed.append(upload.file.read())
        record_provider_attempt()  # fake provider boundary; no network request
        return SimpleNamespace(model_dump=lambda **_: {"status": "indexed", "document": {"source_name": upload.filename}})
    monkeypatch.setattr(documents, "_ingest_upload", ingest)
    assert process_one(db, app(db).state.settings)
    assert observed == [b"synthetic test document"]
    with db.transaction() as session:
        assert session.get(AiJobUpload, job_id) is None
        assert session.get(AiRequestReservation, job_id).provider_attempts == 1
        assert session.get(AiRequestReservation, job_id).state == "succeeded"


def test_expire_unclaimed_removes_attachment_but_never_releases_running(ticket_database):
    from hashlib import sha256
    db = ticket_database
    password = "test-admin-password-123"
    with db.transaction() as session:
        auth = AuthService(session)
        admin = auth.bootstrap_admin(RegisterInput(username="admin", password=password))
        issued = auth.login(LoginInput(username="admin", password=password))
        _, login = auth.authenticate(issued.token)
        reservation, _ = QuotaService(session).reserve(admin.id, "old-upload-key-123", "b" * 64, "upload")
        session.add(AiJob(id=reservation.id, login_session_id=login.id, payload={"private": "temporary"}))
        session.flush()
        content = b"synthetic"
        session.add(AiJobUpload(job_id=reservation.id, content=content, source_name="a.txt", media_type="text/plain",
                                content_sha256=sha256(content).hexdigest()))
        old_id = reservation.id
        reservation.created_at = session.scalar(text("SELECT clock_timestamp() - interval '2 days'"))
    assert expire_unclaimed(db, ttl_seconds=86400) == 1
    with db.transaction() as session:
        assert session.get(AiJobUpload, old_id) is None
        assert session.get(AiJob, old_id).payload == {}
        assert session.get(AiRequestReservation, old_id).state == "failed"
        reservation, _ = QuotaService(session).reserve(admin.id, "running-key-123", "c" * 64, "rag")
        session.add(AiJob(id=reservation.id, login_session_id=login.id, payload={"question": "in progress"}))
        running_id = reservation.id
    assert claim_one(db).id == running_id
    with db.transaction() as session:
        session.get(AiRequestReservation, running_id).created_at = session.scalar(text("SELECT clock_timestamp() - interval '2 days'"))
    assert expire_unclaimed(db, ttl_seconds=86400) == 0
    with db.transaction() as session:
        assert session.get(AiRequestReservation, running_id).state == "running"
