"""Real PostgreSQL tests use only the separately guarded random test database fixture."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
from threading import Barrier

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text

from project_copilot.db.migrations import check_model_drift, migration_config, upgrade
from project_copilot.db.models import LoginSession, RegistrationInvite, Ticket, UserAccount
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.services.auth import AuthService, AuthenticationRejected, RegistrationRejected


ADMIN_PASSWORD = "test-admin-password-9182"
USER_PASSWORD = "test-user-password-9182"


def seed_admin(database):
    with database.transaction() as session:
        return AuthService(session).bootstrap_admin(RegisterInput(username="admin1", password=ADMIN_PASSWORD))


def issue(database):
    with database.transaction() as session:
        return AuthService(session).issue_invitation("admin1", ADMIN_PASSWORD)


def test_upgrade_existing_ticket_preserves_rows_and_matches_model(postgres_database):
    db = postgres_database
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0002_approvals")
        ticket_id = connection.execute(text("INSERT INTO tickets (title, description) VALUES ('旧工单', '应保留') RETURNING id")).scalar_one()
        context_id = connection.execute(text("INSERT INTO agent_contexts (token_hash, expires_at) VALUES (:digest, now() + interval '1 hour') RETURNING id"), {"digest": "a" * 64}).scalar_one()
    upgrade(db)
    upgrade(db)
    check_model_drift(db)
    with db.transaction() as session:
        assert session.get(Ticket, ticket_id).title == "旧工单"
        assert session.scalar(text("SELECT count(*) FROM agent_contexts WHERE id=:id"), {"id": context_id}) == 1
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(migration_config()).get_current_head()
        assert session.scalar(select(func.count()).select_from(UserAccount)) == 0
    assert db.engine.pool.checkedout() == 0


def test_register_invitation_session_expiry_and_revocation(ticket_database):
    db = ticket_database
    admin = seed_admin(db)
    invitation = issue(db)
    with db.transaction() as session:
        service = AuthService(session)
        user = service.register(RegisterInput(username="Alice", password=USER_PASSWORD, invitation=invitation))
        assert user.username == "alice" and user.role == "user" and admin.role == "admin"
    with db.transaction() as session:
        stored = session.scalar(select(UserAccount).where(UserAccount.username == "alice"))
        record = session.scalar(select(RegistrationInvite))
        assert stored.password_hash.startswith("$argon2id$") and USER_PASSWORD not in stored.password_hash
        assert record.token_hash == sha256(invitation.encode()).hexdigest() and record.uses == 1
    with pytest.raises(RegistrationRejected):
        with db.transaction() as session:
            AuthService(session).register(RegisterInput(username="bob", password=USER_PASSWORD, invitation=invitation))
    with pytest.raises(RegistrationRejected):
        with db.transaction() as session:
            AuthService(session).register(RegisterInput(username="bob", password=USER_PASSWORD))
    with db.transaction() as session:
        opened = AuthService(session).register(RegisterInput(username="bob", password=USER_PASSWORD), open_registration=True)
        assert opened.role == "user"
    for username, password in [("alice", "wrong-password"), ("nobody", USER_PASSWORD)]:
        with pytest.raises(AuthenticationRejected):
            with db.transaction() as session:
                AuthService(session).login(LoginInput(username=username, password=password))
    with db.transaction() as session:
        issued = AuthService(session, ttl_seconds=300).login(LoginInput(username="alice", password=USER_PASSWORD))
    with db.transaction() as session:
        row = session.scalar(select(LoginSession))
        assert row.token_hash == sha256(issued.token.encode()).hexdigest() and issued.token not in row.token_hash
        principal, login = AuthService(session).authenticate(issued.token)
        assert principal.id == user.id and principal.role == "user"
        AuthService(session).logout(login)
    with pytest.raises(AuthenticationRejected):
        with db.transaction() as session:
            AuthService(session).authenticate(issued.token)
    with db.transaction() as session:
        expired = AuthService(session, ttl_seconds=300).login(LoginInput(username="alice", password=USER_PASSWORD))
        row = session.scalar(select(LoginSession).where(LoginSession.token_hash == sha256(expired.token.encode()).hexdigest()))
        row.created_at = AuthService(session).now() - timedelta(minutes=10)
        row.expires_at = AuthService(session).now() - timedelta(minutes=1)
    with pytest.raises(AuthenticationRejected):
        with db.transaction() as session:
            AuthService(session).authenticate(expired.token)
    assert db.engine.pool.checkedout() == 0


def test_invitation_use_and_registration_rollback_together(ticket_database):
    db = ticket_database
    seed_admin(db)
    invite = issue(db)
    with pytest.raises(RuntimeError):
        with db.transaction() as session:
            AuthService(session).register(RegisterInput(username="alice", password=USER_PASSWORD, invitation=invite))
            raise RuntimeError("simulate failure")
    with db.transaction() as session:
        assert session.scalar(select(RegistrationInvite.uses)) == 0
        assert session.scalar(select(func.count()).select_from(UserAccount).where(UserAccount.username == "alice")) == 0
        assert AuthService(session).register(RegisterInput(username="alice", password=USER_PASSWORD, invitation=invite)).role == "user"
    assert db.engine.pool.checkedout() == 0


def test_single_use_invite_cannot_be_used_by_concurrent_registrations(ticket_database):
    db = ticket_database
    seed_admin(db)
    invite = issue(db)
    barrier = Barrier(2)

    def worker(username):
        barrier.wait(timeout=10)
        try:
            with db.transaction() as session:
                AuthService(session).register(RegisterInput(username=username, password=USER_PASSWORD, invitation=invite))
            return "created"
        except RegistrationRejected:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["alice", "bob"]))
    assert sorted(results) == ["created", "rejected"]
    with db.transaction() as session:
        assert session.scalar(select(RegistrationInvite.uses)) == 1
        assert session.scalar(select(func.count()).select_from(UserAccount).where(UserAccount.role == "user")) == 1
    assert db.engine.pool.checkedout() == 0


def test_role_management_requires_admin_password_and_preserves_last_admin(ticket_database):
    db = ticket_database
    seed_admin(db)
    with db.transaction() as session:
        AuthService(session).register(RegisterInput(username="alice", password=USER_PASSWORD), open_registration=True)
    for admin, password, target, role in [("admin1", "wrong", "alice", "admin"),
                                          ("alice", USER_PASSWORD, "alice", "admin"),
                                          ("admin1", ADMIN_PASSWORD, "admin1", "user")]:
        with pytest.raises(RegistrationRejected):
            with db.transaction() as session:
                AuthService(session).set_role(admin, password, target, role)
    with db.transaction() as session:
        assert AuthService(session).set_role("admin1", ADMIN_PASSWORD, "alice", "admin").role == "admin"
    with db.transaction() as session:
        assert AuthService(session).set_role("alice", USER_PASSWORD, "admin1", "user").role == "user"
    with pytest.raises(RegistrationRejected):
        with db.transaction() as session:
            AuthService(session).set_role("alice", USER_PASSWORD, "alice", "user")
    assert db.engine.pool.checkedout() == 0


def test_concurrent_duplicate_username_rolls_back_losing_invite(ticket_database, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from project_copilot.api import auth as auth_api
    from project_copilot.config import Settings
    from project_copilot.services import auth as auth_service

    db = ticket_database
    seed_admin(db)
    invites = [issue(db), issue(db)]
    app = FastAPI()
    app.state.database = db
    app.state.settings = Settings(_env_file=None, environment="test", auth_public_origin="https://copilot.example.test")
    app.include_router(auth_api.router)
    barrier = Barrier(2)
    original_hash = auth_service.hash_password

    def synchronized_hash(password):
        barrier.wait(timeout=15)
        return original_hash(password)

    monkeypatch.setattr(auth_service, "hash_password", synchronized_hash)

    def worker(invite):
        with TestClient(app, base_url="https://copilot.example.test") as http:
            response = http.post("/auth/register", headers={"Origin": "https://copilot.example.test"},
                                 json={"username": "same-name", "password": USER_PASSWORD, "invitation": invite})
            assert USER_PASSWORD not in response.text and invite not in response.text
            return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, invites))
    assert sorted(results) == [201, 400]
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(UserAccount).where(UserAccount.username == "same-name")) == 1
        counts = list(session.scalars(select(RegistrationInvite.uses).order_by(RegistrationInvite.uses)))
        assert counts == [0, 1]
    assert db.engine.pool.checkedout() == 0


def test_logout_and_sensitive_write_serialize_on_session(ticket_database):
    from threading import Event

    db = ticket_database
    with db.transaction() as session:
        AuthService(session).register(RegisterInput(username="alice", password=USER_PASSWORD), open_registration=True)
    with db.transaction() as session:
        token = AuthService(session).login(LoginInput(username="alice", password=USER_PASSWORD)).token
    held, release = Event(), Event()

    def logout_worker():
        with db.transaction() as session:
            service = AuthService(session)
            _, row = service.authenticate(token, for_update=True)
            service.logout(row)
            held.set()
            assert release.wait(timeout=10)

    def sensitive_worker():
        try:
            with db.transaction() as session:
                AuthService(session).authenticate(token, for_update=True)
            return "accepted"
        except AuthenticationRejected:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        logout = pool.submit(logout_worker)
        assert held.wait(timeout=10)
        operation = pool.submit(sensitive_worker)
        assert not operation.done()
        release.set()
        logout.result(timeout=10)
        assert operation.result(timeout=10) == "rejected"
    assert db.engine.pool.checkedout() == 0


def test_session_expiring_while_waiting_for_user_lock_is_rejected(ticket_database):
    from threading import Event
    import time

    db = ticket_database
    with db.transaction() as session:
        user = AuthService(session).register(RegisterInput(username="alice", password=USER_PASSWORD), open_registration=True)
    with db.transaction() as session:
        token = AuthService(session).login(LoginInput(username="alice", password=USER_PASSWORD)).token
        record = session.scalar(select(LoginSession).where(LoginSession.token_hash == sha256(token.encode()).hexdigest()))
        record.expires_at = AuthService(session).now() + timedelta(seconds=1)
    entered = Event()

    def authenticated_worker():
        try:
            with db.transaction() as session:
                entered.set()
                AuthService(session).authenticate(token, for_update=True)
            return "accepted"
        except AuthenticationRejected:
            return "expired"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.transaction() as session:
            session.scalar(select(UserAccount).where(UserAccount.id == user.id).with_for_update())
            future = pool.submit(authenticated_worker)
            assert entered.wait(timeout=10)
            time.sleep(1.2)
            assert not future.done(), "authentication must wait for user row lock"
        assert future.result(timeout=10) == "expired"
    assert db.engine.pool.checkedout() == 0


def test_admin_demotion_serializes_with_invitation_issuance(ticket_database):
    from threading import Event

    db = ticket_database
    seed_admin(db)
    with db.transaction() as session:
        service = AuthService(session)
        service.register(RegisterInput(username="alice", password=USER_PASSWORD), open_registration=True)
    with db.transaction() as session:
        AuthService(session).set_role("admin1", ADMIN_PASSWORD, "alice", "admin")
    held, release = Event(), Event()

    def demote_worker():
        with db.transaction() as session:
            AuthService(session).set_role("admin1", ADMIN_PASSWORD, "alice", "user")
            held.set()
            assert release.wait(timeout=10)

    def invite_worker():
        try:
            with db.transaction() as session:
                AuthService(session).issue_invitation("alice", USER_PASSWORD)
            return "issued"
        except RegistrationRejected:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        demotion = pool.submit(demote_worker)
        assert held.wait(timeout=10)
        invitation = pool.submit(invite_worker)
        assert not invitation.done()
        release.set()
        demotion.result(timeout=10)
        assert invitation.result(timeout=10) == "rejected"
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(RegistrationInvite)) == 0
        assert session.scalar(select(UserAccount.role).where(UserAccount.username == "alice")) == "user"
    assert db.engine.pool.checkedout() == 0
