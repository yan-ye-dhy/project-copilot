"""Actual PostgreSQL release control; synthetic users and dependency probes only.

Each test owns a new empty database through ticket_database. No live demo state,
platform service, provider credentials or model request is used.
"""
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from pydantic import SecretStr

from project_copilot.config import Settings
from project_copilot.db.models import UserAccount
from project_copilot.db.release import ReleaseControl, ReleaseEvent
from project_copilot.schemas.auth import RegisterInput
from project_copilot.services.auth import AuthService
from project_copilot.services import release_control as release


@pytest.fixture
def personal_environment(ticket_database, monkeypatch):
    # The dedicated-deployment identity is synthetic; actual test transactions
    # use only ticket_database's freshly-created, isolated database engine.
    settings = Settings(_env_file=None, environment="production", public_demo=True, public_proxy_secret="s" * 48,
                        database_url="postgresql+psycopg://project_copilot_public:synthetic@postgres:5432/project_copilot_public",
                        auth_public_origin="https://personal-demo.edgeone.cool")
    with ticket_database.transaction() as session:
        auth = AuthService(session)
        admin = auth.bootstrap_admin(RegisterInput(username="admin", password="synthetic-personal-password"))
        alice = auth.register(RegisterInput(username="alice", password="synthetic-personal-password"), open_registration=True)
        bob = auth.register(RegisterInput(username="bobby", password="synthetic-personal-password"), open_registration=True)
    monkeypatch.setattr(release, "dependencies_ready", lambda *_: None)
    return ticket_database, settings, admin.id, alice.id, bob.id


def open_personal(environment, *, duration_minutes=90, personal_user_id=None, actor_id=None, evidence=None):
    db, settings, admin, alice, _ = environment
    with db.transaction() as session:
        return release.change_state(
            session, settings, actor_id=actor_id or admin, operation="PERSONAL_DEMO",
            reason="synthetic personal test authorization",
            personal_user_id=personal_user_id or alice, duration_minutes=duration_minutes, evidence=evidence)


def close_personal(environment):
    db, settings, admin, _, _ = environment
    with db.transaction() as session:
        return release.change_state(session, settings, actor_id=admin, operation="CLOSED",
                                    reason="synthetic personal test close")


def test_personal_is_explicit_and_default_remains_closed(personal_environment):
    db, settings, _, alice, _ = personal_environment
    with db.transaction() as session:
        assert release.release_status(session)["state"] == "CLOSED"
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, user_id=alice)
    result = open_personal(personal_environment)
    assert result["state"] == "PERSONAL_DEMO" and result["worker_enabled"] is False
    with db.transaction() as session:
        row = release.require_open(session, settings, user_id=alice)
        now = session.scalar(select(func.clock_timestamp()))
        assert row.allowed_user_ids == [str(alice)]
        assert now < row.expires_at <= now + timedelta(minutes=90)
        assert row.evidence["profile"] == "personal-demo-v1"
        assert UUID(row.evidence["grant_id"])
        assert "checks" not in row.evidence
        assert session.scalar(select(func.count()).select_from(ReleaseEvent)) == 1


@pytest.mark.parametrize("minutes", [0, -1, 91, 1000, True, 1.5])
def test_personal_rejects_invalid_lifetime_without_audit_or_state_write(personal_environment, minutes):
    db, _, _, _, _ = personal_environment
    with pytest.raises((ValueError, release.ReleaseClosed)):
        open_personal(personal_environment, duration_minutes=minutes)
    with db.transaction() as session:
        assert release.release_status(session)["state"] == "CLOSED"
        assert session.scalar(select(func.count()).select_from(ReleaseEvent)) == 0


def test_personal_requires_active_admin_and_existing_active_test_user(personal_environment):
    db, _, admin, alice, bob = personal_environment
    with pytest.raises(release.ReleaseClosed):
        open_personal(personal_environment, actor_id=bob)
    with pytest.raises((ValueError, release.ReleaseClosed)):
        open_personal(personal_environment, personal_user_id=uuid4())
    with db.transaction() as session:
        session.get(UserAccount, alice).active = False
    with pytest.raises((ValueError, release.ReleaseClosed)):
        open_personal(personal_environment)
    with db.transaction() as session:
        assert release.release_status(session)["state"] == "CLOSED"
        session.get(UserAccount, admin).active = False
    with pytest.raises(release.ReleaseClosed):
        open_personal(personal_environment, personal_user_id=bob)


def test_personal_does_not_consume_or_accept_staging_evidence(personal_environment):
    with pytest.raises((ValueError, release.ReleaseClosed)):
        open_personal(personal_environment, evidence={"checks": {"synthetic": "not-a-proof"}})
    result = open_personal(personal_environment)
    assert result["state"] == "PERSONAL_DEMO"
    db, settings, admin, _, _ = personal_environment
    with pytest.raises((ValueError, release.ReleaseClosed)):
        with db.transaction() as session:
            release.change_state(session, settings, actor_id=admin, operation="PUBLIC",
                                 reason="synthetic forbidden promotion", evidence={})
    with db.transaction() as session:
        assert release.release_status(session)["state"] == "PERSONAL_DEMO"


def test_personal_rejects_open_registration_and_ordinary_local_configuration(personal_environment):
    db, settings, admin, alice, _ = personal_environment
    for changed in ({"auth_open_registration": True}, {"public_demo": False, "environment": "development"},
                    {"environment": "development"},
                    {"database_url": SecretStr("postgresql+psycopg://local:synthetic@localhost:5432/reference_db")}):
        with pytest.raises((ValueError, release.ReleaseClosed)):
            with db.transaction() as session:
                release.change_state(session, settings.model_copy(update=changed), actor_id=admin,
                                     operation="PERSONAL_DEMO", personal_user_id=alice, duration_minutes=90,
                                     reason="synthetic invalid personal configuration")


def test_personal_rejects_other_users_and_disables_on_account_deactivation(personal_environment):
    db, settings, _, alice, bob = personal_environment
    open_personal(personal_environment)
    with db.transaction() as session:
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, user_id=bob)
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, user_id=alice, worker=True)
    with db.transaction() as session:
        session.get(UserAccount, alice).active = False
    with db.transaction() as session:
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings)


def test_personal_cannot_renew_open_or_expired_grant_without_explicit_close(personal_environment):
    db, _, _, _, _ = personal_environment
    open_personal(personal_environment)
    with pytest.raises((ValueError, release.ReleaseClosed)):
        open_personal(personal_environment)
    with db.transaction() as session:
        session.execute(text("UPDATE release_control SET expires_at=clock_timestamp()-interval '1 second' WHERE id=1"))
    with pytest.raises((ValueError, release.ReleaseClosed)):
        open_personal(personal_environment)
    close_personal(personal_environment)
    assert open_personal(personal_environment)["state"] == "PERSONAL_DEMO"


def test_expiry_closes_api_worker_and_worker_enable(personal_environment):
    db, settings, admin, alice, _ = personal_environment
    open_personal(personal_environment, duration_minutes=1)
    with db.transaction() as session:
        release.change_state(session, settings, actor_id=admin, operation="worker-enable",
                             reason="synthetic enable personal worker")
        assert release.require_open(session, settings, user_id=alice, worker=True)
    with db.transaction() as session:
        session.execute(text("UPDATE release_control SET expires_at=clock_timestamp()-interval '1 second' WHERE id=1"))
    with db.transaction() as session:
        for arguments in ({}, {"user_id": alice}, {"user_id": alice, "worker": True}):
            with pytest.raises(release.ReleaseClosed):
                release.require_open(session, settings, **arguments)
    with pytest.raises(release.ReleaseClosed):
        with db.transaction() as session:
            release.change_state(session, settings, actor_id=admin, operation="worker-enable",
                                 reason="synthetic expired worker enable")


def test_personal_binds_entry_configuration_and_backend_bytes(personal_environment, monkeypatch):
    db, settings, _, alice, _ = personal_environment
    open_personal(personal_environment)
    with db.transaction() as session:
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings.model_copy(update={"max_daily_requests_per_user": 99}), user_id=alice)
    with monkeypatch.context() as controlled:
        controlled.setattr(release, "code_fingerprint", lambda: "changed-backend")
        with db.transaction() as session:
            with pytest.raises(release.ReleaseClosed):
                release.require_open(session, settings, user_id=alice)
    with db.transaction() as session:
        row = session.get(ReleaseControl, 1)
        row.evidence = {**row.evidence, "source_sha256": {"synthetic": "changed-entry"}}
    with db.transaction() as session:
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, user_id=alice)


def test_personal_reservations_cannot_cross_grants_or_replay_in_staging(personal_environment):
    db, settings, _, alice, _ = personal_environment
    open_personal(personal_environment)
    with db.transaction() as session:
        row = release.require_open(session, settings, user_id=alice)
        old_grant = UUID(row.evidence["grant_id"])
        reservation = SimpleNamespace(user_id=alice, personal_demo_grant_id=old_grant)
        assert release.require_open(session, settings, user_id=alice, reservation=reservation)
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, user_id=alice,
                                 reservation=SimpleNamespace(user_id=alice, personal_demo_grant_id=None))
    close_personal(personal_environment)
    open_personal(personal_environment)
    with db.transaction() as session:
        assert release.require_open(session, settings, user_id=alice).evidence["grant_id"] != str(old_grant)
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, user_id=alice, reservation=reservation)
        # Synthetic direct DB fixture models a later valid STAGING; even there,
        # a task bound to a PERSONAL grant cannot silently resume.
        row = session.get(ReleaseControl, 1)
        row.state, row.origin = "STAGING", "https://personal.test"
        row.evidence = {"checks": {key: "a" * 64 for key in release._STAGING},
                        "configuration_fingerprint": release.config_fingerprint(settings.model_copy(update={"auth_public_origin": "https://personal.test"}))}
    with db.transaction() as session:
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings.model_copy(update={"auth_public_origin": "https://personal.test"}),
                                 user_id=alice, reservation=reservation)


def test_personal_authorization_rolls_back_and_schema_enforces_real_state(ticket_database, personal_environment):
    db, settings, admin, alice, _ = personal_environment
    with pytest.raises(RuntimeError):
        with db.transaction() as session:
            release.change_state(session, settings, actor_id=admin, operation="PERSONAL_DEMO",
                                 personal_user_id=alice, duration_minutes=90,
                                 reason="synthetic rollback authorization")
            raise RuntimeError("controlled rollback")
    with db.transaction() as session:
        assert release.release_status(session)["state"] == "CLOSED"
        assert session.scalar(select(func.count()).select_from(ReleaseEvent)) == 0
    schema = inspect(db.engine)
    columns = {c["name"]: c for c in schema.get_columns("release_control")}
    events = {c["name"]: c for c in schema.get_columns("release_events")}
    assert columns["state"]["type"].length >= len("PERSONAL_DEMO")
    assert events["before_state"]["type"].length >= len("PERSONAL_DEMO")
    assert events["after_state"]["type"].length >= len("PERSONAL_DEMO")
    assert "personal_demo_grant_id" in {c["name"] for c in schema.get_columns("ai_request_reservations")}
    from project_copilot.db.migrations import check_model_drift
    check_model_drift(db)
    with pytest.raises(IntegrityError):
        with db.transaction() as session:
            session.execute(text("UPDATE release_control SET state='ARBITRARY' WHERE id=1"))
