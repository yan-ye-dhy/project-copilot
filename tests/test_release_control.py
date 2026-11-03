"""Release admission under real isolated PostgreSQL; synthetic identity/artifacts only."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Event, Thread

import pytest
from sqlalchemy import func, select, text

from project_copilot.config import Settings
from project_copilot.db.release import ReleaseControl, ReleaseEvent
from project_copilot.schemas.auth import RegisterInput
from project_copilot.services.auth import AuthService
from project_copilot.services import release_control as release


def setup_release(database, tmp_path, monkeypatch):
    settings = Settings(_env_file=None, public_demo=True, public_proxy_secret="a" * 48,
                        auth_public_origin="https://demo.example")
    with database.transaction() as session:
        auth = AuthService(session)
        admin = auth.bootstrap_admin(RegisterInput(username="admin", password="synthetic-password-for-tests"))
        alice = auth.register(RegisterInput(username="alice", password="synthetic-password-for-tests"), open_registration=True)
        bob = auth.register(RegisterInput(username="bobby", password="synthetic-password-for-tests"), open_registration=True)
    artifact = tmp_path / "proof.json"
    artifact.write_text('{"synthetic_only": true}')
    now = datetime.now(timezone.utc)
    evidence = {"format": 1, "state": "STAGING", "resource_id": settings.public_resource_id,
                "code_fingerprint": release.code_fingerprint(), "origin": "https://demo.example",
                "configuration_fingerprint": release.config_fingerprint(settings),
                "verified_at": (now - timedelta(minutes=1)).isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(), "allowed_user_ids": [str(alice.id)],
                "checks": {key: {"path": str(artifact), "sha256": sha256(artifact.read_bytes()).hexdigest()} for key in release._STAGING}}
    monkeypatch.setattr(release, "dependencies_ready", lambda *_: None)
    return settings, admin, alice, bob, evidence


def test_default_closed_requires_durable_authorization(ticket_database):
    settings = Settings(_env_file=None, public_demo=True, public_proxy_secret="a" * 48,
                        auth_public_origin="https://demo.example")
    with ticket_database.transaction() as session:
        assert release.release_status(session)["state"] == "CLOSED"
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, worker=True)


def test_staging_restricts_users_and_close_disables_worker(ticket_database, tmp_path, monkeypatch):
    db = ticket_database
    settings, admin, alice, bob, evidence = setup_release(db, tmp_path, monkeypatch)
    with db.transaction() as session:
        release.change_state(session, settings, actor_id=admin.id, operation="STAGING", reason="synthetic stage acceptance", evidence=evidence)
    with db.transaction() as session:
        release.require_open(session, settings, user_id=alice.id)
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, user_id=bob.id)
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, user_id=alice.id, worker=True)
    with db.transaction() as session:
        release.change_state(session, settings, actor_id=admin.id, operation="worker-enable", reason="synthetic worker enable")
    with db.transaction() as session:
        release.require_open(session, settings, user_id=alice.id, worker=True)
        release.change_state(session, settings, actor_id=admin.id, operation="CLOSED", reason="synthetic emergency close")
    with db.transaction() as session:
        assert release.release_status(session)["worker_enabled"] is False
        assert session.scalar(select(func.count()).select_from(ReleaseEvent)) == 3
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, user_id=alice.id, worker=True)


def test_bad_artifact_code_resource_and_expiry_fail_closed(ticket_database, tmp_path, monkeypatch):
    db = ticket_database
    settings, admin, alice, _, evidence = setup_release(db, tmp_path, monkeypatch)
    for changed in ({"code_fingerprint": "0" * 64}, {"configuration_fingerprint": "0" * 64}, {"resource_id": "wrong-resource"},
                    {"expires_at": datetime.now(timezone.utc).isoformat()}, {"allowed_user_ids": []}):
        with pytest.raises((ValueError, release.ReleaseClosed)):
            with db.transaction() as session:
                release.change_state(session, settings, actor_id=admin.id, operation="STAGING", reason="synthetic stage acceptance", evidence={**evidence, **changed})
    with db.transaction() as session:
        release.change_state(session, settings, actor_id=admin.id, operation="STAGING", reason="synthetic stage acceptance", evidence=evidence)
    monkeypatch.setattr(release, "code_fingerprint", lambda: "changed")
    with db.transaction() as session:
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, user_id=alice.id)


def test_approved_configuration_change_closes_admission(ticket_database, tmp_path, monkeypatch):
    db = ticket_database
    settings, admin, alice, _, evidence = setup_release(db, tmp_path, monkeypatch)
    with db.transaction() as session:
        release.change_state(session, settings, actor_id=admin.id, operation="STAGING", reason="synthetic stage acceptance", evidence=evidence)
        release.change_state(session, settings, actor_id=admin.id, operation="worker-enable", reason="synthetic worker enable")
    variants = ({"max_daily_requests_per_user": 6}, {"auth_session_ttl_seconds": 7200},
                {"public_upload_max_chars": 64000}, {"public_job_queue_ttl_seconds": 7200},
                {"deepseek_max_tokens": 2048}, {"final_rerank_device": "cpu", "final_rerank_dtype": "float32"})
    for changed in variants:
        with db.transaction() as session:
            with pytest.raises(release.ReleaseClosed):
                release.require_open(session, settings.model_copy(update=changed), user_id=alice.id, worker=True)
    with db.transaction() as session:
        release.require_open(session, settings, user_id=alice.id, worker=True)


def test_configuration_fingerprint_excludes_keys_and_passwords():
    from pydantic import SecretStr
    original = Settings(_env_file=None)
    rotated = original.model_copy(update={"deepseek_api_key": SecretStr("synthetic-secret-one"),
                                          "zhipu_api_key": SecretStr("synthetic-secret-two"),
                                          "database_url": SecretStr("postgresql+psycopg://test:private-password@postgres:5432/test"),
                                          "public_proxy_secret": SecretStr("synthetic-proxy-secret")})
    assert release.config_fingerprint(original) == release.config_fingerprint(rotated)
    assert len(release.config_fingerprint(original)) == 64


def test_execution_lease_prevents_maintenance_until_runner_returns(ticket_database):
    db = ticket_database
    entered, finish = Event(), Event()
    def running():
        with release.execution_lease(db):
            entered.set()
            assert finish.wait(5)
    thread = Thread(target=running)
    thread.start()
    try:
        assert entered.wait(5)
        with db.engine.connect() as connection:
            assert connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": release.WORKER_EXECUTION_LOCK}) is False
    finally:
        finish.set()
        thread.join(5)
    with db.engine.connect() as connection:
        assert connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": release.WORKER_EXECUTION_LOCK}) is True
        connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": release.WORKER_EXECUTION_LOCK})


def test_worker_stops_new_attempts_after_closed_without_replaying(ticket_database, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from project_copilot.db.jobs import AiJob
    from project_copilot.db.models import AiRequestReservation
    from project_copilot.infrastructure.call_accounting import record_provider_attempt
    from project_copilot.schemas.auth import LoginInput
    from project_copilot.services import ai_worker
    from project_copilot.services.quotas import QuotaService
    db = ticket_database
    settings, admin, alice, _, evidence = setup_release(db, tmp_path, monkeypatch)
    monkeypatch.setattr(ai_worker, "dependencies_ready", lambda *_: None)
    with db.transaction() as session:
        issued = AuthService(session).login(LoginInput(username="alice", password="synthetic-password-for-tests"))
        _, login = AuthService(session).authenticate(issued.token)
        reservation, _ = QuotaService(session).reserve(alice.id, "release-job-key", "a" * 64, "rag")
        session.add(AiJob(id=reservation.id, login_session_id=login.id, payload={"question": "synthetic"}))
        job_id = reservation.id
    assert ai_worker.process_one(db, settings, runner=lambda *_: pytest.fail("closed worker executed")) is False
    with db.transaction() as session:
        assert session.get(AiRequestReservation, job_id).state == "reserved"
        release.change_state(session, settings, actor_id=admin.id, operation="STAGING", reason="synthetic stage acceptance", evidence=evidence)
        release.change_state(session, settings, actor_id=admin.id, operation="worker-enable", reason="synthetic worker enable")
    attempts = []
    def runner(*_):
        record_provider_attempt()
        attempts.append("first")
        with db.transaction() as session:
            release.change_state(session, settings, actor_id=admin.id, operation="CLOSED", reason="synthetic emergency close")
        record_provider_attempt()
        attempts.append("unreachable")
        return SimpleNamespace(model_dump=lambda **_: {}, generation=None)
    assert ai_worker.process_one(db, settings, runner=runner)
    assert attempts == ["first"]
    assert ai_worker.process_one(db, settings, runner=lambda *_: pytest.fail("closed worker replayed")) is False
    with db.transaction() as session:
        reservation = session.get(AiRequestReservation, job_id)
        assert reservation.provider_attempts == 1 and reservation.state == "unknown"


def test_actual_gateway_staging_checks_proxy_session_allowlist_and_dependencies(ticket_database, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from project_copilot.infrastructure.public_gateway import PublicGatewayMiddleware
    from project_copilot.schemas.auth import LoginInput
    db = ticket_database
    settings, admin, alice, bob, evidence = setup_release(db, tmp_path, monkeypatch)
    with db.transaction() as session:
        auth = AuthService(session)
        tokens = {name: auth.login(LoginInput(username=name, password="synthetic-password-for-tests")).token for name in ("alice", "bobby")}
        release.change_state(session, settings, actor_id=admin.id, operation="STAGING", reason="synthetic stage acceptance", evidence=evidence)
    app = FastAPI()
    app.state.settings, app.state.database = settings, db
    app.add_middleware(PublicGatewayMiddleware)
    reached = []
    @app.get("/protected")
    def protected():
        reached.append(True)
        return {"ok": True}
    headers = {"X-Project-Copilot-Proxy-Secret": "a" * 48}
    with TestClient(app) as client:
        assert client.get("/protected", cookies={"__Host-pc_session": tokens["alice"]}).status_code == 403
        assert client.get("/protected", headers=headers).status_code == 401
        assert client.get("/protected", headers=headers, cookies={"__Host-pc_session": tokens["bobby"]}).status_code == 503
        assert client.get("/protected", headers=headers, cookies={"__Host-pc_session": tokens["alice"]}).status_code == 200
        assert reached == [True]
        def failed(*_):
            raise release.ReleaseClosed("controlled unhealthy dependency")
        monkeypatch.setattr(release, "dependencies_ready", failed)
        assert client.get("/protected", headers=headers, cookies={"__Host-pc_session": tokens["alice"]}).status_code == 503
        assert reached == [True]
    with db.transaction() as session:
        release.change_state(session, settings, actor_id=admin.id, operation="CLOSED", reason="synthetic emergency close")
    with TestClient(app) as client:
        assert client.get("/protected", headers=headers, cookies={"__Host-pc_session": tokens["alice"]}).status_code == 503


def test_close_is_compatible_with_upload_role_lock_and_blocks_demotion(ticket_database, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from project_copilot.db.models import UserAccount
    db = ticket_database
    settings, admin, _, _, _ = setup_release(db, tmp_path, monkeypatch)
    closing, release_close, demoting = Event(), Event(), Event()
    def close_with_operator_share():
        with db.transaction() as session:
            release.change_state(session, settings, actor_id=admin.id, operation="CLOSED", reason="synthetic emergency close")
            closing.set()
            assert release_close.wait(5)
    def demote():
        with db.transaction() as session:
            demoting.set()
            session.execute(text("UPDATE user_accounts SET active=false WHERE id=:id"), {"id": admin.id})
    with ThreadPoolExecutor(max_workers=2) as executor:
        # Simulate the upload's long-lived FOR SHARE role-stability lock.
        with db.transaction() as upload:
            upload.scalar(select(UserAccount).where(UserAccount.id == admin.id).with_for_update(read=True))
            closed = executor.submit(close_with_operator_share)
            try:
                assert closing.wait(5), "close incorrectly waited for upload role lock"
                changed = executor.submit(demote)
                assert demoting.wait(5)
                assert not changed.done(), "administrator demotion bypassed shared role lock"
            finally:
                release_close.set()
            closed.result(timeout=5)
        changed.result(timeout=5)
    with db.transaction() as session:
        assert session.get(UserAccount, admin.id).active is False
        assert release.release_status(session)["state"] == "CLOSED"


def test_admin_rag_auth_and_close_follow_compatible_lock_order(ticket_database, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from project_copilot.schemas.auth import LoginInput
    db = ticket_database
    settings, admin, _, _, evidence = setup_release(db, tmp_path, monkeypatch)
    evidence["allowed_user_ids"] = [str(admin.id)]
    with db.transaction() as session:
        issued = AuthService(session).login(LoginInput(username="admin", password="synthetic-password-for-tests"))
        _, login = AuthService(session).authenticate(issued.token)
        login_id = login.id
        release.change_state(session, settings, actor_id=admin.id, operation="STAGING", reason="synthetic stage acceptance", evidence=evidence)
    provider_locked, finish_provider, operator_started = Event(), Event(), Event()
    def provider():
        with db.transaction() as session:
            release.require_open(session, settings, user_id=admin.id)
            # Same admin RAG provider path: LoginSession FOR UPDATE and user
            # FOR SHARE must remain compatible with the operator's user lock.
            AuthService(session).authenticate_session_id(login_id, for_update=True)
            provider_locked.set()
            assert finish_provider.wait(5)
    def close():
        # CLI credential checking finishes before its control transaction.
        with db.transaction() as session:
            actor = AuthService(session)._verify_admin("admin", "synthetic-password-for-tests")
            actor_id = actor.id
        operator_started.set()
        with db.transaction() as session:
            return release.change_state(session, settings, actor_id=actor_id, operation="CLOSED", reason="synthetic emergency close")
    with ThreadPoolExecutor(max_workers=2) as executor:
        active = executor.submit(provider)
        assert provider_locked.wait(5)
        closing = executor.submit(close)
        try:
            assert operator_started.wait(5)
            assert not closing.done()
        finally:
            finish_provider.set()
        active.result(timeout=5)
        assert closing.result(timeout=5)["state"] == "CLOSED"
