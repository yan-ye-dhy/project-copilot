"""HTTP job/quota/outbox atomicity in owned PostgreSQL; no broker/model calls."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from project_copilot.api import jobs
from project_copilot.config import Settings
from project_copilot.db.jobs import AiJob, AiJobUpload, AiRequestAttempt
from project_copilot.db.messaging import AiJobOutbox
from project_copilot.db.models import AiRequestReservation
from project_copilot.db.release import ReleaseControl
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.services import ai_worker, messaging, release_control
from project_copilot.services.auth import AuthService, csrf_token

ORIGIN = "https://demo.example"


@pytest.fixture
def mq_api(ticket_database, monkeypatch):
    database = ticket_database
    # Code files may be written concurrently by implementers. These synthetic
    # deployment fingerprints are not live release/entry-protection evidence.
    monkeypatch.setattr(release_control, "code_fingerprint", lambda: "synthetic-mq-code")
    monkeypatch.setattr(release_control, "config_fingerprint", lambda _settings: "synthetic-mq-config")
    monkeypatch.setattr(ai_worker, "dependencies_ready", lambda *_: None)
    monkeypatch.setattr(release_control, "dependencies_ready", lambda *_: None)
    settings = Settings(_env_file=None, public_demo=True,
        public_proxy_secret="synthetic-mq-secret-" * 3, auth_public_origin=ORIGIN,
        database_url=database.engine.url.render_as_string(hide_password=False),
        message_queue_enabled=True, rabbitmq_url="amqp://synthetic:synthetic@127.0.0.1:5672/%2F",
        max_daily_requests_per_user=20, max_daily_requests_global=50,
        max_concurrent_requests_per_user=20, tracing_enabled=False)
    with database.transaction() as session:
        auth = AuthService(session)
        auth.bootstrap_admin(RegisterInput(username="mqadmin", password="synthetic-mq-password-123"))
        token = auth.login(LoginInput(username="mqadmin", password="synthetic-mq-password-123")).token
        release = session.get(ReleaseControl, 1)
        release.state, release.worker_enabled = "PUBLIC", True
        release.resource_id, release.origin = settings.public_resource_id, ORIGIN
        release.code_fingerprint = "synthetic-mq-code"
        release.expires_at = session.scalar(select(func.clock_timestamp())) + timedelta(hours=1)
        release.evidence = {"checks": {key: "a" * 64 for key in release_control._PUBLIC},
                            "configuration_fingerprint": "synthetic-mq-config"}
    application = FastAPI()
    application.state.database, application.state.settings = database, settings
    application.include_router(jobs.router)
    with TestClient(application, base_url=ORIGIN) as client:
        client.cookies.set("__Host-pc_session", token)
        yield SimpleNamespace(database=database, application=application, client=client,
            headers=lambda key: {"Origin": ORIGIN, "X-CSRF-Token": csrf_token(token),
                                 "Idempotency-Key": key})


@pytest.mark.parametrize("kind", ["rag", "agent"])
def test_http_idempotency_commits_one_job_reservation_and_outbox_event(mq_api, kind):
    setup = mq_api
    body = {"kind": kind, "payload": {"question": "受控持久消息问题"}}
    options = {"json": body, "headers": setup.headers("mq-api-idempotent-key")}
    first = setup.client.post("/jobs", **options)
    assert first.status_code == 202
    replay = setup.client.post("/jobs", **options)
    assert replay.status_code == 202 and replay.json()["job_id"] == first.json()["job_id"]
    conflict = setup.client.post("/jobs", headers=options["headers"],
        json={"kind": kind, "payload": {"question": "不同的问题"}})
    assert conflict.status_code == 409
    identity = UUID(first.json()["job_id"])
    with setup.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiJob)) == 1
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 1
        events = session.scalars(select(AiJobOutbox)).all()
        assert len(events) == 1 and events[0].job_id == identity
        assert events[0].dispatch_generation == 1 and events[0].publish_attempts == 0
        assert events[0].sent_at is None
        attempts = session.scalars(select(AiRequestAttempt.outcome).order_by(AiRequestAttempt.created_at)).all()
        assert attempts == ["accepted", "reused", "failed"]


@pytest.mark.parametrize("failure_point", ["outbox_insert", "after_outbox_insert"])
def test_enqueue_failure_rolls_back_job_quota_and_outbox_together(mq_api, monkeypatch, failure_point):
    setup = mq_api
    inserted = []
    original_enqueue, original_audit = messaging.enqueue_outbox, jobs.record_attempt

    if failure_point == "outbox_insert":
        def failing_enqueue(session, job_id, **kwargs):
            assert session.get(AiJob, job_id) is not None
            assert session.get(AiRequestReservation, job_id) is not None
            raise SQLAlchemyError("synthetic outbox insert failure")
        monkeypatch.setattr(messaging, "enqueue_outbox", failing_enqueue)
    else:
        def failing_audit(session, user_id, kind, outcome, reservation_id=None):
            if outcome == "accepted":
                event = session.scalar(select(AiJobOutbox).where(AiJobOutbox.job_id == reservation_id))
                assert event is not None
                inserted.append(event.event_id)
                raise SQLAlchemyError("synthetic failure after outbox insert")
            return original_audit(session, user_id, kind, outcome, reservation_id)
        monkeypatch.setattr(jobs, "record_attempt", failing_audit)
    response = setup.client.post("/jobs", headers=setup.headers("mq-api-rollback-key"),
        json={"kind": "rag", "payload": {"question": "原子事务失败"}})
    assert response.status_code == 503
    with setup.database.transaction() as session:
        for model in (AiJob, AiRequestReservation, AiJobOutbox):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert session.scalars(select(AiRequestAttempt.outcome)).all() == ["failed"]
    if failure_point == "after_outbox_insert":
        assert len(inserted) == 1
    # The identical key can create its first reservation after rollback.
    monkeypatch.setattr(messaging, "enqueue_outbox", original_enqueue)
    monkeypatch.setattr(jobs, "record_attempt", original_audit)
    retry = setup.client.post("/jobs", headers=setup.headers("mq-api-rollback-key"),
        json={"kind": "rag", "payload": {"question": "原子事务失败"}})
    assert retry.status_code == 202
    with setup.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiJobOutbox)) == 1
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 1


def test_upload_idempotency_stores_attachment_and_one_dispatch_atomically(mq_api):
    setup = mq_api
    options = {"headers": setup.headers("mq-upload-idempotent-key"),
               "files": {"file": ("synthetic.txt", b"controlled upload text", "text/plain")}}
    first = setup.client.post("/jobs/upload", **options)
    assert first.status_code == 202
    replay = setup.client.post("/jobs/upload", **options)
    assert replay.status_code == 202 and replay.json()["job_id"] == first.json()["job_id"]
    identity = UUID(first.json()["job_id"])
    with setup.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiJobOutbox)) == 1
        assert session.scalar(select(func.count()).select_from(AiJobUpload)) == 1
        assert session.get(AiJobUpload, identity).content == b"controlled upload text"
        assert session.get(AiRequestReservation, identity).kind == "upload"


def test_upload_outbox_failure_rolls_back_attachment_and_reservation(mq_api, monkeypatch):
    setup = mq_api
    original_enqueue = messaging.enqueue_outbox
    inserted = []

    def fail_after_insert(session, job_id, **kwargs):
        event = original_enqueue(session, job_id, **kwargs)
        assert session.get(AiJobUpload, job_id) is not None
        inserted.append(event.event_id)
        raise SQLAlchemyError("synthetic upload dispatch transaction failure")

    monkeypatch.setattr(messaging, "enqueue_outbox", fail_after_insert)
    response = setup.client.post("/jobs/upload", headers=setup.headers("mq-upload-rollback-key"),
        files={"file": ("synthetic.txt", b"controlled upload text", "text/plain")})
    assert response.status_code == 503 and len(inserted) == 1
    with setup.database.transaction() as session:
        for model in (AiJob, AiJobUpload, AiRequestReservation, AiJobOutbox):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_disabled_messaging_preserves_existing_postgres_worker_path(mq_api):
    setup = mq_api
    setup.application.state.settings = setup.application.state.settings.model_copy(
        update={"message_queue_enabled": False, "rabbitmq_url": None})
    response = setup.client.post("/jobs", headers=setup.headers("mq-disabled-postgres-key"),
        json={"kind": "rag", "payload": {"question": "沿用数据库执行"}})
    assert response.status_code == 202
    calls = []

    def runner(_settings, _database, job):
        calls.append(job.id)
        return SimpleNamespace(model_dump=lambda **_: {"answer": "数据库路径答案"}, generation=None)

    assert ai_worker.process_one(setup.database, setup.application.state.settings, runner=runner)
    assert not ai_worker.process_one(setup.database, setup.application.state.settings, runner=runner)
    assert calls == [UUID(response.json()["job_id"])]
    result = setup.client.get("/jobs/" + response.json()["job_id"])
    assert result.status_code == 200 and result.json()["result"] == {"answer": "数据库路径答案"}
    with setup.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiJobOutbox)) == 0

