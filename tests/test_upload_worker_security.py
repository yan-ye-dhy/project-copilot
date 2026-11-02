"""Upload worker lock/permission tests using isolated real PostgreSQL only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import sleep
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text

from project_copilot.api import documents
from project_copilot.db.models import AiRequestReservation
from project_copilot.db.jobs import AiJobUpload
from project_copilot.infrastructure.call_accounting import record_provider_attempt
from project_copilot.services.ai_worker import claim_one, execute, process_one
from test_public_jobs import app, headers
from test_public_upload_jobs import identities


def test_upload_worker_keeps_role_and_session_stable_without_accounting_deadlock(ticket_database, monkeypatch):
    db = ticket_database
    tokens = identities(db)
    application = app(db)
    token = tokens["admin1"]
    with TestClient(application) as client:
        submitted = client.post("/jobs/upload", cookies={"__Host-pc_session": token},
                                headers=headers(token, "worker-lock-test"),
                                files={"file": ("test.txt", b"hello", "text/plain")})
        assert submitted.status_code == 202
    job = claim_one(db)
    assert job is not None and job.kind == "upload"
    entered, release, demote_started, revoke_started = Event(), Event(), Event(), Event()

    def fake_ingest(*_):
        record_provider_attempt()
        entered.set()
        assert release.wait(timeout=10)
        # A separate accounting transaction must not deadlock on the session
        # and user rows already held by the worker's indexing transaction.
        record_provider_attempt()
        return "completed"

    monkeypatch.setattr(documents, "_ingest_upload", fake_ingest)

    def demote():
        with db.transaction() as session:
            demote_started.set()
            session.execute(text("UPDATE user_accounts SET role='user' WHERE id=:id"), {"id": job.user_id})

    def revoke():
        with db.transaction() as session:
            revoke_started.set()
            session.execute(text("UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE id=:id"), {"id": job.session_id})

    with ThreadPoolExecutor(max_workers=3) as pool:
        running = pool.submit(execute, application.state.settings, db, job)
        try:
            assert entered.wait(timeout=10)
            demotion, revocation = pool.submit(demote), pool.submit(revoke)
            assert demote_started.wait(timeout=5) and revoke_started.wait(timeout=5)
            sleep(0.2)
            assert not demotion.done() and not revocation.done()
        finally:
            release.set()
        assert running.result(timeout=10) == "completed"
        demotion.result(timeout=10); revocation.result(timeout=10)
    with db.transaction() as session:
        assert session.get(AiRequestReservation, job.id).provider_attempts == 2


def test_concurrent_upload_workers_claim_once_and_remove_attachment(ticket_database):
    db = ticket_database
    tokens = identities(db)
    application = app(db)
    token = tokens["admin1"]
    with TestClient(application) as client:
        submitted = client.post("/jobs/upload", cookies={"__Host-pc_session": token},
                                headers=headers(token, "worker-claim-test"),
                                files={"file": ("test.txt", b"hello", "text/plain")})
        assert submitted.status_code == 202
        job_id = UUID(submitted.json()["job_id"])
    entered, release = Event(), Event()
    calls = []

    def runner(_settings, _db, job):
        calls.append(job.id)
        entered.set()
        assert release.wait(timeout=10)
        return SimpleNamespace(model_dump=lambda **_: {"status": "indexed"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(process_one, db, application.state.settings, runner=runner)
        try:
            assert entered.wait(timeout=10)
            second = pool.submit(process_one, db, application.state.settings, runner=runner)
            assert second.result(timeout=10) is False
            with db.transaction() as session:
                assert session.get(AiJobUpload, job_id) is None
                assert session.get(AiRequestReservation, job_id).state == "running"
        finally:
            release.set()
        assert first.result(timeout=10) is True
    assert len(calls) == 1 and calls[0] == job_id
    with db.transaction() as session:
        assert session.get(AiRequestReservation, job_id).state == "succeeded"
