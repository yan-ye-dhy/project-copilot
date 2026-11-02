"""Authentication limits use isolated, real PostgreSQL databases only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, text

from project_copilot.api.auth import router
from project_copilot.config import Settings
from project_copilot.db.models import AuthRateBucket, UserAccount
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.services.auth import AuthService, AuthenticationRejected
from project_copilot.services.auth_rate import account_key, consume_auth_attempt


SECRET = "Q" * 43
ORIGIN = "https://copilot.example.test"


def reserve(database, username):
    with database.transaction() as session:
        return consume_auth_attempt(session, username=username, secret=SECRET)


def test_account_attempts_are_atomic_and_window_resets(ticket_database):
    database = ticket_database
    barrier = Barrier(8)

    def concurrent_attempt(_):
        barrier.wait(timeout=10)
        return reserve(database, "alice")

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(concurrent_attempt, range(8)))
    assert outcomes.count(True) == 5 and outcomes.count(False) == 3
    with database.transaction() as session:
        rows = list(session.scalars(select(AuthRateBucket)))
        assert len(rows) == 2 and all("alice" not in row.bucket_key for row in rows)
        assert next(row.attempts for row in rows if row.scope == "account") == 8
        session.execute(text("UPDATE auth_rate_buckets SET window_started=clock_timestamp() - interval '2 minutes'"))
    assert reserve(database, "alice") is True


def test_global_window_blocks_username_flood_atomically(ticket_database):
    database = ticket_database
    with database.transaction() as session:
        session.execute(text("INSERT INTO auth_rate_buckets VALUES ('global','global',date_trunc('minute',clock_timestamp()),58)"))
    barrier = Barrier(4)

    def concurrent_attempt(index):
        barrier.wait(timeout=10)
        return reserve(database, f"user{index}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(concurrent_attempt, range(4)))
    assert outcomes.count(True) == 2 and outcomes.count(False) == 2
    with database.transaction() as session:
        assert len(list(session.scalars(select(AuthRateBucket).where(AuthRateBucket.scope == "account")))) == 2


def test_failed_login_attempts_commit_before_auth_and_ignore_spoofed_ip(ticket_database, monkeypatch):
    database = ticket_database
    app = FastAPI()
    settings = Settings(_env_file=None, environment="test", auth_public_origin=ORIGIN)
    app.state.settings = settings.model_copy(update={"public_demo": True, "public_proxy_secret": SecretStr(SECRET)})
    app.state.database = database
    app.include_router(router)
    calls = []

    def rejected(self, payload):
        calls.append(payload.username)
        raise AuthenticationRejected()

    monkeypatch.setattr(AuthService, "login", rejected)
    with TestClient(app, base_url=ORIGIN) as client:
        for index in range(6):
            response = client.post("/auth/login", headers={"Origin": ORIGIN, "X-Forwarded-For": f"192.0.2.{index}"},
                                   json={"username": " Alice ", "password": "fake-password-value"})
            assert response.status_code == (401 if index < 5 else 429)
            assert "fake-password-value" not in response.text
        assert len(calls) == 5 and set(calls) == {"alice"}
        assert response.headers["cache-control"] == "no-store"
    with database.transaction() as session:
        assert session.get(AuthRateBucket, ("account", account_key(SECRET, "alice"))).attempts == 6


def test_hash_concurrency_is_bounded_across_database_sessions(ticket_database, monkeypatch):
    database = ticket_database
    app = FastAPI()
    settings = Settings(_env_file=None, environment="test", auth_public_origin=ORIGIN)
    app.state.settings = settings.model_copy(update={"public_demo": True, "public_proxy_secret": SecretStr(SECRET)})
    app.state.database = database
    app.include_router(router)
    calls = []

    def rejected(self, payload):
        calls.append(payload.username)
        raise AuthenticationRejected()

    monkeypatch.setattr(AuthService, "login", rejected)
    with database.engine.connect() as first, database.engine.connect() as second:
        first.execute(text("SELECT pg_advisory_xact_lock(1578566001, 0)"))
        second.execute(text("SELECT pg_advisory_xact_lock(1578566001, 1)"))
        with TestClient(app, base_url=ORIGIN) as client:
            response = client.post("/auth/login", headers={"Origin": ORIGIN},
                                   json={"username": "alice", "password": "example-password"})
            assert response.status_code == 429 and not calls
        first.rollback()
        with TestClient(app, base_url=ORIGIN) as client:
            response = client.post("/auth/login", headers={"Origin": ORIGIN},
                                   json={"username": "bob", "password": "example-password"})
            assert response.status_code == 401 and calls == ["bob"]


def test_public_login_without_database_fails_closed():
    app = FastAPI()
    settings = Settings(_env_file=None, environment="test", auth_public_origin=ORIGIN)
    app.state.settings = settings.model_copy(update={"public_demo": True, "public_proxy_secret": SecretStr(SECRET)})
    app.include_router(router)
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post("/auth/login", headers={"Origin": ORIGIN},
                               json={"username": "alice", "password": "example-password"})
    assert response.status_code == 503
    assert "example-password" not in response.text


def test_server_session_id_rechecks_revocation_and_user_active(ticket_database):
    database = ticket_database
    with database.transaction() as session:
        AuthService(session).bootstrap_admin(RegisterInput(username="admin1", password="test-admin-password-9182"))
    with database.transaction() as session:
        issued = AuthService(session).login(LoginInput(username="admin1", password="test-admin-password-9182"))
        _, row = AuthService(session).authenticate(issued.token)
        session_id = row.id
    with database.transaction() as session:
        service = AuthService(session)
        principal, row = service.authenticate_session_id(session_id, for_update=True)
        assert principal.username == "admin1"
        service.logout(row)
    with database.transaction() as session:
        for value in (session_id, uuid4(), str(session_id)):
            try:
                AuthService(session).authenticate_session_id(value)
            except AuthenticationRejected:
                pass
            else:
                raise AssertionError("revoked or invalid session ID accepted")
    with database.transaction() as session:
        issued = AuthService(session).login(LoginInput(username="admin1", password="test-admin-password-9182"))
        principal, row = AuthService(session).authenticate(issued.token)
        active_id = row.id
        session.get(UserAccount, principal.id).active = False
    with database.transaction() as session:
        try:
            AuthService(session).authenticate_session_id(active_id)
        except AuthenticationRejected:
            pass
        else:
            raise AssertionError("disabled user's session was accepted")
