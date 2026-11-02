"""Authentication unit tests use controlled services and never touch real data."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID
import secrets

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from project_copilot.api import auth
from project_copilot.config import Settings
from project_copilot.schemas.auth import RegisterInput, UserRead
from project_copilot.services.auth import (
    AuthenticationRejected, IssuedSession, Principal, csrf_matches, csrf_token,
    hash_password, verify_password,
)


USER_ID = UUID("12345678-1234-4234-8234-123456789012")
ORIGIN = "https://copilot.example.test"
PASSWORD = "not-a-real-password-9281"
INVITE = "I" * 43


def test_argon2id_passwords_are_salted_and_never_match_wrong_password():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first.startswith("$argon2id$") and second.startswith("$argon2id$")
    assert first != second and PASSWORD not in first and verify_password(first, PASSWORD)
    assert not verify_password(first, "different-password")
    assert not verify_password("broken-hash", PASSWORD)


def test_csrf_is_session_bound_and_rejects_malformed_values():
    a, b = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    csrf = csrf_token(a)
    assert len(csrf) == 64 and csrf_matches(a, csrf)
    assert not csrf_matches(b, csrf) and not csrf_matches(a, "x" * 64)
    assert not csrf_matches(a, "中" * 64) and not csrf_matches("invalid", csrf)
    assert not csrf_matches(a, None)


def test_registration_schema_normalizes_username_and_forbids_client_role():
    payload = RegisterInput(username=" Alice_01 ", password=PASSWORD, invitation=INVITE)
    assert payload.username == "alice_01" and payload.password.get_secret_value() == PASSWORD
    assert PASSWORD not in repr(payload) and INVITE not in repr(payload)
    with pytest.raises(ValidationError):
        RegisterInput(username="alice", password=PASSWORD, role="admin")
    for invalid in ["../../bad", "用戶名", "ab", "bad@name"]:
        with pytest.raises(ValidationError):
            RegisterInput(username=invalid, password=PASSWORD)
    for invalid in ["short", " " * 20, "x" * 1025]:
        with pytest.raises(ValidationError):
            RegisterInput(username="alice", password=invalid)


class FakeService:
    ttl_seconds = 86400

    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.revoked = False
        self.login_calls = 0
        self.register_calls = 0
        self.logout_calls = 0
        self.user = UserRead(id=USER_ID, username="alice", role="user")
        self.row = SimpleNamespace()

    def register(self, payload, *, open_registration=False):
        self.register_calls += 1
        if not open_registration and (payload.invitation is None or payload.invitation.get_secret_value() != INVITE):
            raise auth.RegistrationRejected()
        return self.user

    def login(self, payload):
        self.login_calls += 1
        if payload.username != "alice" or payload.password.get_secret_value() != PASSWORD:
            raise AuthenticationRejected()
        return IssuedSession(user=self.user, token=self.token, expires_at=datetime.now(timezone.utc) + timedelta(days=1))

    def authenticate(self, token, *, for_update=False):
        if token != self.token or self.revoked:
            raise AuthenticationRejected()
        return Principal(id=USER_ID, username="alice", role="user"), self.row

    def logout(self, row):
        assert row is self.row
        self.logout_calls += 1
        self.revoked = True


def client(public=False, *, origin=ORIGIN):
    app = FastAPI()
    settings = Settings(_env_file=None, environment="test", auth_public_origin=origin,
                        auth_open_registration=False, auth_session_ttl_seconds=86400)
    # Test secure cookies using a real Pydantic HttpUrl instance from Settings.
    if public:
        settings = settings.model_copy(update={"public_demo": True})
    app.state.settings = settings
    svc = FakeService()

    def fake_service():
        try:
            yield svc
        except AuthenticationRejected:
            raise HTTPException(401, "账号、密码或会话无效") from None
        except auth.RegistrationRejected:
            raise HTTPException(400, "注册信息或邀请码无效") from None

    app.dependency_overrides[auth.service] = fake_service
    app.include_router(auth.router)
    return TestClient(app, base_url=ORIGIN, raise_server_exceptions=True), svc


def test_register_requires_invitation_and_never_allows_self_assigned_admin():
    with client()[0] as http:
        base = {"username": "alice", "password": PASSWORD}
        missing = http.post("/auth/register", headers={"Origin": ORIGIN}, json=base)
        assert missing.status_code == 400
        elevated = http.post("/auth/register", headers={"Origin": ORIGIN}, json={**base, "invitation": INVITE, "role": "admin"})
        assert elevated.status_code == 422 and PASSWORD not in elevated.text and INVITE not in elevated.text
        response = http.post("/auth/register", headers={"Origin": ORIGIN}, json={**base, "invitation": INVITE})
        assert response.status_code == 201 and response.json()["role"] == "user"
        assert response.headers["cache-control"] == "no-store"
        assert PASSWORD not in response.text and INVITE not in response.text


def test_origin_is_required_for_login_registration_and_logout(monkeypatch):
    # Origin behavior is tested with a fake auth service; PostgreSQL rate
    # limiting and hashing slots have separate real-database integration tests.
    monkeypatch.setattr(auth, "_limit_public_attempt", lambda *_: None)
    monkeypatch.setattr(auth, "_acquire_hash_slot", lambda *_: None)
    http, svc = client(public=True)
    with http:
        body = {"username": "alice", "password": PASSWORD}
        for value in (None, "https://evil.example", "null", "http://copilot.example.test"):
            headers = {"Origin": value} if value is not None else {}
            response = http.post("/auth/login", headers=headers, json=body)
            assert response.status_code == 403 and response.headers["cache-control"] == "no-store"
        assert svc.login_calls == 0
        result = http.post("/auth/login", headers={"Origin": ORIGIN}, json=body)
        assert result.status_code == 200
        csrf = result.json()["csrf_token"]
        assert http.post("/auth/logout", headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf}).status_code == 403
        assert svc.logout_calls == 0


def test_production_cookie_is_host_only_secure_and_logout_revokes(monkeypatch):
    monkeypatch.setattr(auth, "_limit_public_attempt", lambda *_: None)
    monkeypatch.setattr(auth, "_acquire_hash_slot", lambda *_: None)
    http, svc = client(public=True)
    with http:
        response = http.post("/auth/login", headers={"Origin": ORIGIN}, json={"username": "alice", "password": PASSWORD})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        cookie = response.headers["set-cookie"]
        assert "__Host-pc_session=" in cookie and "HttpOnly" in cookie and "Secure" in cookie
        assert "SameSite=lax" in cookie and "Path=/" in cookie and "Domain=" not in cookie
        assert svc.token not in response.text and PASSWORD not in response.text
        csrf = response.json()["csrf_token"]
        current = http.get("/auth/me")
        assert current.status_code == 200 and current.json()["csrf_token"] == csrf
        assert current.json()["user"]["id"] == str(USER_ID)
        assert http.post("/auth/logout", headers={"Origin": ORIGIN}).status_code == 403
        assert http.post("/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": "0" * 64}).status_code == 403
        assert svc.logout_calls == 0
        logout = http.post("/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
        assert logout.status_code == 200 and logout.json() == {"status": "ok"}
        assert "Max-Age=0" in logout.headers["set-cookie"] and svc.logout_calls == 1
        assert http.get("/auth/me").status_code == 401


def test_local_cookie_remains_httponly_and_lax_without_secure():
    http, _ = client(public=False)
    with http:
        response = http.post("/auth/login", headers={"Origin": ORIGIN}, json={"username": "alice", "password": PASSWORD})
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("pc_session=") and "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert "Secure" not in cookie


def test_duplicate_cookie_and_duplicate_origin_are_rejected():
    http, svc = client(public=True)
    with http:
        body = {"username": "alice", "password": PASSWORD}
        duplicate = http.post("/auth/login", headers=[("Origin", ORIGIN), ("Origin", ORIGIN)], json=body)
        assert duplicate.status_code == 403 and svc.login_calls == 0
        result = http.get("/auth/me", headers={"Cookie": f"__Host-pc_session={svc.token}; __Host-pc_session={svc.token}"})
        assert result.status_code == 401


def test_validation_failures_and_invalid_json_never_echo_secrets():
    http, _ = client()
    with http:
        for body in (
            {"username": "alice", "password": PASSWORD, "invitation": INVITE, "role": "admin"},
            {"username": "bad/name", "password": PASSWORD, "invitation": INVITE},
            [PASSWORD, INVITE],
        ):
            response = http.post("/auth/register", headers={"Origin": ORIGIN}, json=body)
            assert response.status_code == 422
            assert PASSWORD not in response.text and INVITE not in response.text
            assert response.headers["cache-control"] == "no-store"
        malformed = http.post("/auth/register", headers={"Origin": ORIGIN, "Content-Type": "application/json"},
                              content='{"password":"' + PASSWORD)
        assert malformed.status_code == 422 and PASSWORD not in malformed.text
        assert malformed.headers["cache-control"] == "no-store"


def test_public_auth_origin_fails_closed_if_missing_or_not_https():
    http, _ = client(public=True)
    with http:
        http.app.state.settings = http.app.state.settings.model_copy(update={"auth_public_origin": None})
        result = http.post("/auth/login", headers={"Origin": ORIGIN}, json={"username": "alice", "password": PASSWORD})
        assert result.status_code == 503
        http.app.state.settings = http.app.state.settings.model_copy(update={"auth_public_origin": "http://copilot.example.test"})
        result = http.post("/auth/login", headers={"Origin": ORIGIN}, json={"username": "alice", "password": PASSWORD})
        assert result.status_code == 503
