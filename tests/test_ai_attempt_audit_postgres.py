"""Persistent, sanitized submission audit against isolated real PostgreSQL."""
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from project_copilot.api import jobs as jobs_api
from project_copilot.db.jobs import AiJob, AiJobUpload, AiRequestAttempt
from project_copilot.db.models import AiRequestReservation
from test_public_jobs import app, headers, prepare
from test_public_upload_jobs import identities


def test_job_outcomes_are_durable_and_never_store_payload_or_credentials(ticket_database):
    db = ticket_database
    token = prepare(db)["alice"]
    application = app(db)
    cookie = {"__Host-pc_session": token}
    secret_question = "PRIVATE_AUDIT_QUESTION"
    body = {"kind": "rag", "payload": {"question": secret_question}}
    with TestClient(application) as client:
        first = client.post("/jobs", cookies=cookie, headers=headers(token, "audit-first-key"), json=body)
        assert first.status_code == 202
        job_id = UUID(first.json()["job_id"])
        assert client.post("/jobs", cookies=cookie, headers=headers(token, "audit-first-key"), json=body).json()["job_id"] == str(job_id)
        assert client.post("/jobs", cookies=cookie, headers=headers(token, "audit-second-key"), json=body).status_code == 429
        assert client.post("/jobs", cookies=cookie, headers=headers(token, "audit-first-key"),
                           json={"kind": "rag", "payload": {"question": "different"}}).status_code == 409
        assert client.post("/jobs", cookies=cookie, headers=headers(token, "audit-bad-payload"),
                           json={"kind": "rag", "payload": {"question": " "}}).status_code == 422
    with db.transaction() as session:
        rows = list(session.scalars(select(AiRequestAttempt).order_by(AiRequestAttempt.created_at)))
        assert [row.outcome for row in rows] == ["accepted", "reused", "quota_rejected", "failed", "failed"]
        assert [row.reservation_id for row in rows] == [job_id, job_id, None, None, None]
        assert all(row.kind == "rag" for row in rows)
        assert secret_question not in repr([row.__dict__ for row in rows])
        assert token not in repr([row.__dict__ for row in rows])
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 1
        reservation = session.get(AiRequestReservation, job_id)
        assert reservation.provider_attempts == 0 and reservation.usage is None and reservation.cost_status == "unknown"


def test_audit_write_failure_fails_closed_and_rolls_back_accepted_job(ticket_database, monkeypatch):
    db = ticket_database
    token = prepare(db)["alice"]
    application = app(db)

    def unavailable(*_args, **_kwargs):
        raise SQLAlchemyError("PRIVATE_SQL_EXCEPTION")

    monkeypatch.setattr(jobs_api, "record_attempt", unavailable)
    with TestClient(application) as client:
        response = client.post("/jobs", cookies={"__Host-pc_session": token},
                               headers=headers(token, "audit-failure-key"),
                               json={"kind": "rag", "payload": {"question": "PRIVATE_QUESTION"}})
        assert response.status_code == 503 and "PRIVATE_SQL_EXCEPTION" not in response.text
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 0
        assert session.scalar(select(func.count()).select_from(AiJob)) == 0
        assert session.scalar(select(func.count()).select_from(AiRequestAttempt)) == 0


def test_quota_rejection_audit_failure_returns_503_not_successful_denial(ticket_database, monkeypatch):
    db = ticket_database
    token = prepare(db)["alice"]
    application = app(db)
    with TestClient(application) as client:
        payload = {"kind": "rag", "payload": {"question": "test"}}
        assert client.post("/jobs", cookies={"__Host-pc_session": token}, headers=headers(token, "first-audit-key"), json=payload).status_code == 202
        original = jobs_api.record_attempt

        def unavailable(session, user_id, kind, outcome, reservation_id=None):
            if outcome == "quota_rejected":
                raise SQLAlchemyError("PRIVATE_SQL_EXCEPTION")
            return original(session, user_id, kind, outcome, reservation_id)

        monkeypatch.setattr(jobs_api, "record_attempt", unavailable)
        response = client.post("/jobs", cookies={"__Host-pc_session": token}, headers=headers(token, "second-audit-key"), json=payload)
        assert response.status_code == 503 and "PRIVATE_SQL_EXCEPTION" not in response.text


def test_upload_success_replay_validation_and_quota_are_audited(ticket_database):
    db = ticket_database
    token = identities(db)["admin1"]
    application = app(db)
    cookie = {"__Host-pc_session": token}
    data = {"file": ("PRIVATE_FILENAME.txt", b"PRIVATE_FILE_CONTENT", "text/plain")}
    with TestClient(application) as client:
        first = client.post("/jobs/upload", cookies=cookie, headers=headers(token, "upload-first-key"), files=data)
        assert first.status_code == 202
        assert client.post("/jobs/upload", cookies=cookie, headers=headers(token, "upload-first-key"), files=data).status_code == 202
        assert client.post("/jobs/upload", cookies=cookie, headers=headers(token, "upload-second-key"), files=data).status_code == 429
        assert client.post("/jobs/upload", cookies=cookie, headers=headers(token, "upload-invalid-key"),
                           files={"file": ("unsupported.exe", b"x", "application/octet-stream")}).status_code == 415
    with db.transaction() as session:
        rows = list(session.scalars(select(AiRequestAttempt).order_by(AiRequestAttempt.created_at)))
        assert [row.outcome for row in rows] == ["accepted", "reused", "quota_rejected", "failed"]
        serialized = repr([row.__dict__ for row in rows])
        assert "PRIVATE_FILENAME" not in serialized and "PRIVATE_FILE_CONTENT" not in serialized and token not in serialized
        assert session.scalar(select(func.count()).select_from(AiJobUpload)) == 1
