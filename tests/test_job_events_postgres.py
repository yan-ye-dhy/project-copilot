"""Real isolated PostgreSQL event/auth tests; release admission is controlled.

These router-contract tests stub only require_open, not Session/Cookie identity,
ownership, storage, sequence allocation or migration. No live grant or provider.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from project_copilot.api import job_events
from project_copilot.api.jobs import router
from project_copilot.config import Settings
from project_copilot.db.jobs import AiJob, AiJobEvent
from project_copilot.db.migrations import check_model_drift, upgrade
from project_copilot.db.models import AiRequestReservation, LoginSession, UserAccount
from project_copilot.db.operations import JobContentTombstone, JobRecoveryAudit
from project_copilot.infrastructure.job_progress import LABELS
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.services.auth import AuthService
from project_copilot.services.job_events import EventWriter, MAX_EVENTS, append_event, recent_events
from project_copilot.services.release_control import ReleaseClosed


@pytest.fixture
def event_setup(ticket_database, monkeypatch):
    database = ticket_database
    application = FastAPI()
    application.state.database = database
    application.state.settings = Settings(_env_file=None, public_demo=True,
        public_proxy_secret="synthetic-event-secret-" * 3,
        auth_public_origin="https://demo.example")
    application.include_router(router)
    # Explicit synthetic admission; actual release tests remain elsewhere.
    monkeypatch.setattr(job_events, "require_open", lambda *_args, **_kwargs: None)
    with database.transaction() as session:
        auth = AuthService(session)
        people = {}
        for username in ("alice", "bob"):
            user = auth.register(RegisterInput(username=username, password="event-test-password-12345"),
                                 open_registration=True)
            issued = auth.login(LoginInput(username=username, password="event-test-password-12345"))
            _, login = auth.authenticate(issued.token)
            people[username] = SimpleNamespace(id=user.id, token=issued.token, session_id=login.id)

    def create(*, status="succeeded", owner="alice", events=True):
        person = people[owner]
        with database.transaction() as session:
            reservation = AiRequestReservation(id=uuid4(), user_id=person.id, kind="rag", state=status,
                utc_day=datetime.now(timezone.utc).date(), idempotency_key=str(uuid4()),
                payload_fingerprint="f" * 64,
                finished_at=datetime.now(timezone.utc) if status in {"succeeded", "failed"} else None)
            session.add(reservation)
            session.flush()
            job = AiJob(id=reservation.id, login_session_id=person.session_id,
                        payload={"question": "synthetic"},
                        result={"answer": "verified final"} if status == "succeeded" else None)
            session.add(job)
            session.flush()
            if events:
                append_event(session, job.id, "progress", {"stage": "running", "label": LABELS["running"]})
                append_event(session, job.id, "delta", {"text": "provisional"})
                if status in {"succeeded", "failed", "unknown"}:
                    append_event(session, job.id, "terminal", {"status": status})
            return job.id

    return SimpleNamespace(database=database, app=application, people=people, create=create)


def request_for(setup, token):
    request = Request({"type": "http", "method": "GET", "path": "/jobs/events",
        "query_string": b"", "headers": [(b"cookie", ("__Host-pc_session=" + token).encode())],
        "app": setup.app})
    async def connected():
        return False
    request.is_disconnected = connected
    return request


def sse(setup, job_id, *, owner="alice", query="", headers=None):
    with TestClient(setup.app, base_url="https://demo.example") as client:
        client.cookies.set("__Host-pc_session", setup.people[owner].token)
        return client.get(f"/jobs/{job_id}/events{query}", headers=headers or {})


def test_empty_migration_and_event_schema_match_orm(ticket_database):
    upgrade(ticket_database)
    check_model_drift(ticket_database)
    columns = {row["name"] for row in inspect(ticket_database.engine).get_columns("ai_job_events")}
    assert columns == {"job_id", "sequence", "kind", "data", "created_at"}
    with ticket_database.transaction() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "0015_personal_capacity"


def test_sequence_cap_and_expiry_keep_reservation_identity(event_setup):
    setup = event_setup
    job_id = setup.create(events=False)
    with setup.database.transaction() as session:
        for i in range(MAX_EVENTS + 5):
            append_event(session, job_id, "delta", {"text": str(i)})
    with setup.database.transaction() as session:
        events = recent_events(session, job_id)
        assert len(events) == MAX_EVENTS
        assert [row.sequence for row in events] == list(range(6, MAX_EVENTS + 6))
        assert session.get(AiJob, job_id).event_sequence == MAX_EVENTS + 5
        oldest = events[0]
        oldest.created_at = datetime.now(timezone.utc) - timedelta(days=8)
    with setup.database.transaction() as session:
        assert len(recent_events(session, job_id)) == MAX_EVENTS - 1
        assert session.get(AiRequestReservation, job_id).state == "succeeded"


def test_concurrent_writers_assign_unique_gap_free_sequences(event_setup):
    setup = event_setup
    job_id = setup.create(events=False)
    def write(number):
        with setup.database.transaction() as session:
            return append_event(session, job_id, "delta", {"text": str(number)}).sequence
    with ThreadPoolExecutor(max_workers=4) as executor:
        values = list(executor.map(write, range(20)))
    assert sorted(values) == list(range(1, 21))
    with setup.database.transaction() as session:
        assert [event.sequence for event in recent_events(session, job_id)] == list(range(1, 21))


@pytest.mark.parametrize("kind,data", [
    ("progress", {"stage": "model", "label": "untrusted label"}),
    ("delta", {"text": ""}), ("delta", {"text": "x" * 1025}),
    ("diagnostic", {"failure_type": "password and private error"}),
    ("terminal", {"status": "running"}), ("other", {}),
])
def test_invalid_events_are_rejected_without_partial_sequence(event_setup, kind, data):
    setup = event_setup
    job_id = setup.create(events=False)
    with pytest.raises(ValueError), setup.database.transaction() as session:
        append_event(session, job_id, kind, data)
    with setup.database.transaction() as session:
        assert session.get(AiJob, job_id).event_sequence == 0
        assert recent_events(session, job_id) == []


def test_database_rejects_invalid_event_and_rolls_back(event_setup):
    setup = event_setup
    job_id = setup.create(events=False)
    with pytest.raises(IntegrityError), setup.database.transaction() as session:
        session.add(AiJobEvent(job_id=job_id, sequence=1, kind="invalid", data={}))
        session.flush()
    with setup.database.transaction() as session:
        assert recent_events(session, job_id) == []


def test_writer_batches_deltas_bounds_total_and_preserves_unicode(event_setup):
    setup = event_setup
    job_id = setup.create(events=False)
    writer = EventWriter(setup.database, job_id)
    for _ in range(400):
        writer("delta", {"text": "竹"})
    writer("progress", {"stage": "validate", "label": LABELS["validate"]})
    writer("delta", {"text": "山" * 5000})
    writer.flush()
    with setup.database.transaction() as session:
        events = recent_events(session, job_id)
        deltas = [event.data["text"] for event in events if event.kind == "delta"]
        assert "".join(deltas) == "竹" * 400 + "山" * 3600
        assert len(deltas) < 20  # not one transaction per model token
        assert max(map(len, deltas)) <= 512
        assert any(event.kind == "progress" and event.data["stage"] == "validate" for event in events)


def test_terminal_sse_resume_deduplicates_and_canonical_result_remains_authoritative(event_setup):
    setup = event_setup
    job_id = setup.create()
    first = sse(setup, job_id)
    assert first.status_code == 200
    assert first.headers["content-type"].startswith("text/event-stream")
    assert first.headers["cache-control"] == "no-store, no-transform"
    assert first.headers["x-accel-buffering"] == "no"
    assert "id: 1\nevent: progress" in first.text
    assert "id: 2\nevent: delta" in first.text
    assert "id: 3\nevent: terminal" in first.text
    replay = sse(setup, job_id, query="?after=1", headers={"Last-Event-ID": "2"})
    assert "id: 1\n" not in replay.text and "id: 2\n" not in replay.text
    assert "id: 3\nevent: terminal" in replay.text
    with TestClient(setup.app, base_url="https://demo.example") as client:
        client.cookies.set("__Host-pc_session", setup.people["alice"].token)
        result = client.get(f"/jobs/{job_id}").json()
    assert result["status"] == "succeeded" and result["result"] == {"answer": "verified final"}
    with setup.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiJob)) == 1
        assert session.get(AiRequestReservation, job_id).provider_attempts == 0


def test_auth_and_ownership_are_checked_before_event_bytes_even_for_admin(event_setup):
    setup = event_setup
    job_id = setup.create()
    with TestClient(setup.app, base_url="https://demo.example") as client:
        unauthorized = client.get(f"/jobs/{job_id}/events")
    assert unauthorized.status_code == 401 and "provisional" not in unauthorized.text
    assert sse(setup, job_id, owner="bob").status_code == 404
    with setup.database.transaction() as session:
        session.get(UserAccount, setup.people["bob"].id).role = "admin"
    assert sse(setup, job_id, owner="bob").status_code == 404


@pytest.mark.parametrize("query,headers", [
    ("?after=-1", {}), ("?after=1&after=2", {}), ("?extra=secret", {}),
    ("?after=2147483648", {}), ("?after=", {}),
    ("", {"Last-Event-ID": "-1"}), ("", {"Last-Event-ID": "not-an-id"}),
])
def test_invalid_cursors_do_not_open_stream(event_setup, query, headers):
    job_id = event_setup.create()
    assert sse(event_setup, job_id, query=query, headers=headers).status_code == 400


@pytest.mark.parametrize("history", ["expired", "tombstoned", "future", "capped"])
def test_missing_history_resets_provisional_text(event_setup, history):
    setup = event_setup
    job_id = setup.create()
    if history == "expired":
        with setup.database.transaction() as session:
            for event in recent_events(session, job_id):
                event.created_at = datetime.now(timezone.utc) - timedelta(days=8)
    elif history == "tombstoned":
        with setup.database.transaction() as session:
            session.add(JobContentTombstone(job_id=job_id))
    elif history == "capped":
        with setup.database.transaction() as session:
            session.execute(text("DELETE FROM ai_job_events WHERE job_id=:id AND sequence=1"), {"id": job_id})
    response = sse(setup, job_id, query="?after=100" if history == "future" else "")
    assert response.status_code == 200 and "event: reset" in response.text
    assert "provisional" not in response.text
    assert "event: snapshot" in response.text


def test_prejournal_unknown_is_terminal_without_replay_or_fake_failure(event_setup):
    setup = event_setup
    job_id = setup.create(status="unknown", events=False)
    response = sse(setup, job_id)
    assert response.status_code == 200 and '"status":"unknown"' in response.text
    assert "event: delta" not in response.text and "failed" not in response.text
    with setup.database.transaction() as session:
        reservation = session.get(AiRequestReservation, job_id)
        assert reservation.state == "unknown" and reservation.finished_at is None
        assert reservation.provider_attempts == 0
        assert session.get(AiJob, job_id).event_sequence == 0


@pytest.mark.parametrize("revocation", ["session", "release"])
def test_existing_stream_rechecks_revocation_and_release_each_batch(event_setup, monkeypatch, revocation):
    setup = event_setup
    job_id = setup.create(status="running")
    monkeypatch.setattr(job_events, "POLL_SECONDS", 0)
    request = request_for(setup, setup.people["alice"].token)
    async def check():
        response = await job_events.stream_events(request, job_id)
        stream = response.body_iterator
        assert await anext(stream) == ": connected\n\n"
        assert "event: progress" in await anext(stream)
        assert "event: delta" in await anext(stream)
        if revocation == "session":
            with setup.database.transaction() as session:
                session.get(LoginSession, setup.people["alice"].session_id).revoked_at = datetime.now(timezone.utc)
        else:
            def closed(*_args, **_kwargs):
                raise ReleaseClosed()
            monkeypatch.setattr(job_events, "require_open", closed)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    asyncio.run(check())
    with setup.database.transaction() as session:
        assert session.get(AiRequestReservation, job_id).state == "running"


def test_closed_gate_returns_503_before_stream_headers(event_setup, monkeypatch):
    job_id = event_setup.create()
    def closed(*_args, **_kwargs):
        raise ReleaseClosed()
    monkeypatch.setattr(job_events, "require_open", closed)
    response = sse(event_setup, job_id)
    assert response.status_code == 503 and not response.headers["content-type"].startswith("text/event-stream")


def test_controlled_retention_clears_only_old_terminal_events_preserving_unknown_evidence(event_setup):
    from project_copilot.services.public_maintenance import cleanup

    setup = event_setup
    succeeded = setup.create(status="succeeded")
    failed = setup.create(status="failed")
    unknown = setup.create(status="unknown")
    running = setup.create(status="running")
    recovered = setup.create(status="failed")
    with setup.database.transaction() as session:
        admin = session.get(UserAccount, setup.people["bob"].id)
        admin.role = "admin"
        for event in session.scalars(select(AiJobEvent)):
            event.created_at = datetime.now(timezone.utc) - timedelta(days=8)
        session.add(JobRecoveryAudit(job_id=recovered, actor_user_id=admin.id,
            previous_state="unknown", resolution="terminal_no_retry", provider_attempts=0,
            evidence_sha256="a" * 64, inspection_sha256="b" * 64))
    with setup.database.transaction() as session:
        counts = cleanup(session, setup.people["bob"].id)
        assert counts["event_content"] == 6
    with setup.database.transaction() as session:
        remaining = set(session.scalars(select(AiJobEvent.job_id)))
        assert remaining == {unknown, running, recovered}
        assert succeeded not in remaining and failed not in remaining
        assert session.scalar(select(func.count()).select_from(AiJob)) == 5
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 5
        assert session.get(AiRequestReservation, unknown).state == "unknown"
        assert session.get(AiRequestReservation, running).state == "running"
        # Retention hides old drafts even when investigation preserves their DB rows.
        assert recent_events(session, unknown) == []
