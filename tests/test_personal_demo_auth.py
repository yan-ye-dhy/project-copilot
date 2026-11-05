"""Personal auth gates: controlled unit cases plus isolated real PostgreSQL tests."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from project_copilot.api import auth
from project_copilot.config import Settings
from project_copilot.infrastructure.public_gateway import PublicGatewayMiddleware
from project_copilot.schemas.auth import UserRead
from project_copilot.services.auth import AuthenticationRejected, IssuedSession, Principal
from project_copilot.services import release_control as release


ALICE = UUID("00000000-0000-4000-8000-000000000001")
BOB = UUID("00000000-0000-4000-8000-000000000002")
ORIGIN = "https://personal.example"
PASSWORD = "synthetic-auth-password-9172"
SECRET = "synthetic_proxy_" + "a" * 40
TOKEN = "a" * 43


def demo_settings():
    return Settings(_env_file=None, environment="production", public_demo=True,
                    public_proxy_secret=SECRET, auth_public_origin=ORIGIN,
                    database_url="postgresql+psycopg://project_copilot_public:synthetic@postgres:5432/project_copilot_public")


def controlled_auth_client(monkeypatch, *, mode="PERSONAL_DEMO", user_id=ALICE, expire_after_login=False):
    app = FastAPI()
    app.state.settings, app.state.database = demo_settings(), object()
    app.include_router(auth.router)
    calls = []
    svc = SimpleNamespace(session=object(), ttl_seconds=86400)

    def login(payload):
        calls.append("password_verified")
        if payload.password.get_secret_value() != PASSWORD:
            raise AuthenticationRejected()
        return IssuedSession(user=UserRead(id=user_id, username="alice", role="user"), token=TOKEN,
                             expires_at=datetime.now(timezone.utc) + timedelta(days=1))

    svc.login = login
    svc.register = lambda *args, **kwargs: pytest.fail("personal registration reached service")
    svc.authenticate = lambda *args, **kwargs: (Principal(user_id, "alice", "user"), object())

    def dependency():
        try:
            yield svc
        except AuthenticationRejected:
            raise HTTPException(401, "账号、密码或会话无效") from None
        except release.ReleaseClosed:
            raise HTTPException(503, "认证服务暂不可用") from None

    def require_open(session, settings, *, user_id=None, **kwargs):
        calls.append(("gate", user_id))
        if expire_after_login and "password_verified" in calls:
            raise release.ReleaseClosed()
        if mode == "PERSONAL_DEMO" and user_id is not None and user_id != ALICE:
            raise release.ReleaseClosed()
        return SimpleNamespace(state=mode, allowed_user_ids=[str(ALICE)])

    app.dependency_overrides[auth.service] = dependency
    monkeypatch.setattr(release, "require_open", require_open)
    monkeypatch.setattr(auth, "_limit_public_attempt", lambda *_: calls.append("rate_limit"))
    monkeypatch.setattr(auth, "_acquire_hash_slot", lambda *_: calls.append("hash_slot"))
    return TestClient(app, base_url=ORIGIN), calls


def test_personal_allowed_login_keeps_password_rate_cookie_and_csrf_contract(monkeypatch):
    client, calls = controlled_auth_client(monkeypatch)
    with client:
        response = client.post("/auth/login", headers={"Origin": ORIGIN},
                               json={"username": "alice", "password": PASSWORD})
        assert response.status_code == 200
        assert "rate_limit" in calls and "hash_slot" in calls and "password_verified" in calls
        assert ("gate", ALICE) in calls
        cookie = response.headers["set-cookie"]
        assert all(value in cookie for value in ("__Host-pc_session=", "Secure", "HttpOnly", "SameSite=lax", "Path=/"))
        assert "Domain=" not in cookie and response.headers["cache-control"] == "no-store"
        assert PASSWORD not in response.text and TOKEN not in response.text
        assert len(response.json()["csrf_token"]) == 64


def test_personal_wrong_account_after_real_service_identity_has_no_cookie(monkeypatch):
    client, calls = controlled_auth_client(monkeypatch, user_id=BOB)
    with client:
        response = client.post("/auth/login", headers={"Origin": ORIGIN},
                               json={"username": "alice", "password": PASSWORD})
    assert response.status_code == 401 and "set-cookie" not in response.headers
    assert "password_verified" in calls
    assert PASSWORD not in response.text and TOKEN not in response.text


def test_personal_expiry_between_gateway_and_password_result_fails_closed(monkeypatch):
    client, calls = controlled_auth_client(monkeypatch, expire_after_login=True)
    with client:
        response = client.post("/auth/login", headers={"Origin": ORIGIN},
                               json={"username": "alice", "password": PASSWORD})
    assert response.status_code == 503 and "set-cookie" not in response.headers
    assert "password_verified" in calls


def test_personal_invitation_cannot_register_another_account(monkeypatch):
    client, calls = controlled_auth_client(monkeypatch)
    with client:
        response = client.post("/auth/register", headers={"Origin": ORIGIN},
                               json={"username": "alice", "password": PASSWORD, "invitation": "i" * 43})
    assert response.status_code == 403
    assert "rate_limit" not in calls


def test_personal_wrong_origin_still_rejected_before_credentials(monkeypatch):
    client, calls = controlled_auth_client(monkeypatch)
    with client:
        response = client.post("/auth/login", headers={"Origin": "https://wrong.example"},
                               json={"username": "alice", "password": PASSWORD})
    assert response.status_code == 403 and not calls


@pytest.mark.parametrize("mode", ["STAGING", "PUBLIC"])
def test_existing_modes_keep_auth_me_without_personal_identity_restriction(monkeypatch, mode):
    client, calls = controlled_auth_client(monkeypatch, mode=mode, user_id=BOB)
    with client:
        response = client.get("/auth/me", headers={"Cookie": "__Host-pc_session=" + TOKEN})
    assert response.status_code == 200 and response.json()["user"]["id"] == str(BOB)
    assert ("gate", BOB) not in calls


@pytest.mark.parametrize("path,user_id,allowed", [
    ("/health", BOB, True), ("/auth/login", BOB, True),
    ("/auth/register", ALICE, False), ("/auth/me", BOB, False),
    ("/auth/logout", BOB, False), ("/tickets", BOB, False),
    ("/auth/me", ALICE, True), ("/auth/logout", ALICE, True), ("/tickets", ALICE, True),
])
def test_personal_gateway_single_account_includes_me_and_logout(monkeypatch, path, user_id, allowed):
    @contextmanager
    def transaction():
        yield object()
    database = SimpleNamespace(transaction=transaction)
    app = SimpleNamespace(state=SimpleNamespace(database=database, settings=demo_settings()))

    def require_open(session, settings, *, user_id=None, **kwargs):
        if user_id is not None and user_id != ALICE:
            raise release.ReleaseClosed()
        return SimpleNamespace(state="PERSONAL_DEMO", allowed_user_ids=[str(ALICE)])

    monkeypatch.setattr(release, "require_open", require_open)
    monkeypatch.setattr(release, "dependencies_ready", lambda *_: None)
    from project_copilot.services import auth as auth_service
    monkeypatch.setattr(auth_service.AuthService, "authenticate",
                        lambda *args, **kwargs: (Principal(user_id, "synthetic", "user"), object()))
    scope = {"type": "http", "method": "GET", "path": path, "app": app,
             "headers": [(b"cookie", ("__Host-pc_session=" + TOKEN).encode())]}
    if allowed:
        PublicGatewayMiddleware._admit(scope)
    else:
        with pytest.raises(release.ReleaseClosed):
            PublicGatewayMiddleware._admit(scope)


def real_personal_app(database, monkeypatch):
    """The URL is a configuration contract; all I/O uses the random fixture DB."""
    from project_copilot.schemas.auth import LoginInput, RegisterInput
    from project_copilot.services.auth import AuthService
    settings = demo_settings()
    monkeypatch.setattr(release, "dependencies_ready", lambda *_: None)
    with database.transaction() as session:
        service = AuthService(session)
        admin = service.bootstrap_admin(RegisterInput(username="personal_admin", password=PASSWORD))
        alice = service.register(RegisterInput(username="alice", password=PASSWORD), open_registration=True)
        bob = service.register(RegisterInput(username="bobby", password=PASSWORD), open_registration=True)
        bob_token = service.login(LoginInput(username="bobby", password=PASSWORD)).token
        release.change_state(session, settings, actor_id=admin.id, operation="PERSONAL_DEMO",
                             reason="synthetic personal auth test", personal_user_id=alice.id, duration_minutes=90)
    app = FastAPI()
    app.state.settings, app.state.database = settings, database
    app.add_middleware(PublicGatewayMiddleware)
    app.include_router(auth.router)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app, admin, alice, bob, bob_token


def test_postgres_personal_wrong_login_rolls_back_new_session_and_normal_cookie_works(ticket_database, monkeypatch):
    from sqlalchemy import func, select
    from project_copilot.db.models import LoginSession, UserAccount
    app, _, alice, bob, _ = real_personal_app(ticket_database, monkeypatch)
    headers = {"Origin": ORIGIN, "X-Project-Copilot-Proxy-Secret": SECRET}
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/health", headers=headers).status_code == 200
        rejected = client.post("/auth/login", headers=headers,
                               json={"username": "bobby", "password": PASSWORD})
        assert rejected.status_code == 401 and "set-cookie" not in rejected.headers
        accepted = client.post("/auth/login", headers=headers,
                               json={"username": "alice", "password": PASSWORD})
        assert accepted.status_code == 200 and accepted.json()["user"]["id"] == str(alice.id)
        assert all(attr in accepted.headers["set-cookie"] for attr in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"))
        me = client.get("/auth/me", headers=headers)
        assert me.status_code == 200 and me.json()["user"]["id"] == str(alice.id)
        assert client.post("/auth/logout", headers=headers).status_code == 403
        csrf_headers = {**headers, "X-CSRF-Token": accepted.json()["csrf_token"]}
        assert client.post("/auth/logout", headers=csrf_headers).status_code == 200
        assert client.get("/auth/me", headers=headers).status_code == 401
        # A valid invitation-shaped field cannot expand the one-user demo.
        assert client.post("/auth/register", headers=headers, json={
            "username": "newuser", "password": PASSWORD, "invitation": "i" * 43,
        }).status_code == 503
    with ticket_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(LoginSession).where(LoginSession.user_id == bob.id)) == 1
        assert session.scalar(select(func.count()).select_from(UserAccount)) == 3


def test_postgres_personal_other_existing_cookie_cannot_read_me_or_logout(ticket_database, monkeypatch):
    app, _, _, _, bob_token = real_personal_app(ticket_database, monkeypatch)
    headers = {"X-Project-Copilot-Proxy-Secret": SECRET,
               "Cookie": "__Host-pc_session=" + bob_token, "Origin": ORIGIN}
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/auth/me", headers=headers).status_code == 503
        assert client.post("/auth/logout", headers=headers).status_code == 503
        assert client.get("/auth/me", headers={"X-Project-Copilot-Proxy-Secret": SECRET}).status_code == 401


def test_postgres_closed_between_gateway_and_auth_handler_creates_no_session(ticket_database, monkeypatch):
    from sqlalchemy import func, select
    from project_copilot.db.models import LoginSession
    app, admin, alice, _, _ = real_personal_app(ticket_database, monkeypatch)
    original = auth._release_for_auth
    changed = []

    def close_before_handler(request, svc, **kwargs):
        if not changed:
            with ticket_database.transaction() as session:
                release.change_state(session, app.state.settings, actor_id=admin.id,
                                     operation="CLOSED", reason="synthetic close before auth handler")
            changed.append(True)
        return original(request, svc, **kwargs)

    monkeypatch.setattr(auth, "_release_for_auth", close_before_handler)
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post("/auth/login", headers={"Origin": ORIGIN, "X-Project-Copilot-Proxy-Secret": SECRET},
                               json={"username": "alice", "password": PASSWORD})
    assert response.status_code == 503 and "set-cookie" not in response.headers
    assert changed == [True]
    with ticket_database.transaction() as session:
        assert release.release_status(session)["state"] == "CLOSED"
        assert session.scalar(select(func.count()).select_from(LoginSession).where(LoginSession.user_id == alice.id)) == 0


def test_postgres_expiry_after_password_verification_rolls_back_session(ticket_database, monkeypatch):
    from sqlalchemy import func, select
    from project_copilot.db.models import LoginSession
    from project_copilot.db.release import ReleaseControl
    from project_copilot.services.auth import AuthService
    app, _, alice, _, _ = real_personal_app(ticket_database, monkeypatch)
    original = AuthService.login

    def expires_during_login(self, payload):
        issued = original(self, payload)
        row = self.session.get(ReleaseControl, 1)
        row.expires_at = self.now() - timedelta(seconds=1)
        self.session.flush()
        return issued

    monkeypatch.setattr(AuthService, "login", expires_during_login)
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post("/auth/login", headers={"Origin": ORIGIN, "X-Project-Copilot-Proxy-Secret": SECRET},
                               json={"username": "alice", "password": PASSWORD})
    assert response.status_code == 503 and "set-cookie" not in response.headers
    with ticket_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(LoginSession).where(LoginSession.user_id == alice.id)) == 0
