"""Real isolated PostgreSQL policy; no model, live demo or client-selected mode."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from project_copilot.db.migrations import migration_config, upgrade
from project_copilot.db.models import AgentContext, PendingAction, Ticket, UserAccount
from project_copilot.db.operations import MaintenanceAudit
from project_copilot.db.release import ReleaseControl


CAPACITY = {"context_hourly": 100, "context_active": 30, "proposal_daily": 200, "proposal_pending": 30}
LOW = [5, 3, 10, 3]


def grant(session, owner, *, state="PERSONAL_DEMO", limits=None):
    now = session.scalar(select(func.clock_timestamp()))
    row = session.get(ReleaseControl, 1)
    row.state, row.worker_enabled = state, False
    row.resource_id, row.origin = "public-demo-v2", "https://capacity.example"
    row.code_fingerprint = "a" * 64
    row.allowed_user_ids = [str(owner)]
    row.expires_at = now + timedelta(hours=24)
    row.evidence = {"profile": "personal-demo-v2", "grant_id": str(uuid4()), "started_at": now.isoformat(),
                    "configuration_fingerprint": "b" * 64, "source_sha256": "c" * 64,
                    "limits": {**CAPACITY, **(limits or {}), "job_limit": 500}}
    session.flush()
    return row


@pytest.fixture
def capacity(ticket_database):
    with ticket_database.transaction() as session:
        owner = UserAccount(username="capacity_owner", role="admin", password_hash="$argon2id$synthetic-unused")
        other = UserAccount(username="capacity_other", role="user", password_hash="$argon2id$synthetic-unused")
        session.add_all((owner, other))
        session.flush()
        grant(session, owner.id)
        return SimpleNamespace(database=ticket_database, owner=owner.id, other=other.id)


def context(session, owner):
    row = AgentContext(owner_user_id=owner, token_hash="d" * 64,
                       expires_at=datetime.now(timezone.utc) + timedelta(hours=24))
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def proposal(session, owner, context_id):
    row = PendingAction(owner_user_id=owner, context_id=context_id, title="synthetic capacity",
                        description="isolated database only", payload_fingerprint="e" * 64,
                        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15))
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def effective(session, owner, wall=None):
    return session.scalar(text("SELECT personal_operation_limits(:owner, COALESCE(:wall, clock_timestamp()))"),
                          {"owner": owner, "wall": wall})


def test_new_migration_is_repeatable_and_preserves_existing_business_and_audit_rows(postgres_database):
    db = postgres_database
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0014_execution_slots")
    with db.transaction() as session:
        owner = UserAccount(username="before_capacity", role="admin", password_hash="$argon2id$synthetic-unused")
        session.add(owner)
        session.flush()
        ctx = context(session, owner.id)
        action = proposal(session, owner.id, ctx.id)
        ticket = Ticket(owner_user_id=owner.id, title="existing record", description="preserve original business data")
        audit = MaintenanceAudit(actor_user_id=owner.id, counts={"operation": "synthetic-preserved"})
        session.add_all((ticket, audit))
        session.flush()
        identities = owner.id, ctx.id, action.id, ticket.id, audit.id
        baseline = ctx.created_at, ctx.expires_at, action.created_at, action.expires_at, action.payload_fingerprint
    upgrade(db)
    upgrade(db)
    with db.transaction() as session:
        owner_id, context_id, action_id, ticket_id, audit_id = identities
        ctx, action = session.get(AgentContext, context_id), session.get(PendingAction, action_id)
        assert (ctx.created_at, ctx.expires_at, action.created_at, action.expires_at, action.payload_fingerprint) == baseline
        assert session.get(Ticket, ticket_id).title == "existing record"
        assert session.get(MaintenanceAudit, audit_id).counts == {"operation": "synthetic-preserved"}
        assert effective(session, owner_id) == LOW
    with pytest.raises(DBAPIError), db.transaction() as session:
        session.execute(text("UPDATE maintenance_audits SET counts='{}'::jsonb WHERE id=:id"), {"id": audit_id})


def test_only_authorized_owner_receives_personal_snapshot_limits(capacity):
    with capacity.database.transaction() as session:
        assert effective(session, capacity.owner) == [100, 30, 200, 30]
        assert effective(session, capacity.other) == LOW
        assert effective(session, capacity.owner, datetime.now(timezone.utc) + timedelta(days=2)) == LOW
        # User-selected settings cannot turn a foreign identity into the owner.
        session.execute(text("SET LOCAL project_copilot.personal_demo = 'true'"))
        assert effective(session, capacity.other) == LOW


@pytest.mark.parametrize("state", ["CLOSED", "STAGING", "PUBLIC"])
def test_other_modes_keep_existing_context_and_proposal_thresholds(capacity, state):
    db = capacity.database
    with db.transaction() as session:
        grant(session, capacity.owner, state=state)
        assert effective(session, capacity.owner) == LOW
        contexts = [context(session, capacity.owner).id for _ in range(3)]
        for _ in range(3):
            proposal(session, capacity.owner, contexts[0])
    with pytest.raises(DBAPIError), db.transaction() as session:
        context(session, capacity.owner)
    with pytest.raises(DBAPIError), db.transaction() as session:
        proposal(session, capacity.owner, contexts[0])


@pytest.mark.parametrize("damage", ["expired", "future_start", "over_24h", "no_timezone", "bad_date", "wrong_profile",
    "wrong_resource", "multiple_users", "no_allowed_user", "inactive_user", "no_snapshot", "string_value",
    "fraction", "negative", "over_ceiling", "missing_key", "bad_grant", "no_config_fingerprint"])
def test_invalid_or_expired_personal_grants_fail_to_old_database_limits(capacity, damage):
    db = capacity.database
    with db.transaction() as session:
        row = session.get(ReleaseControl, 1)
        evidence = dict(row.evidence)
        if damage == "expired":
            row.expires_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
        elif damage == "future_start":
            evidence["started_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        elif damage == "over_24h":
            row.expires_at += timedelta(seconds=1)
        elif damage == "no_timezone":
            evidence["started_at"] = "2026-11-05T00:00:00"
        elif damage == "bad_date":
            evidence["started_at"] = "invalid+00:00"
        elif damage == "wrong_profile":
            evidence["profile"] = "personal-demo-v1"
        elif damage == "wrong_resource":
            row.resource_id = "development"
        elif damage == "multiple_users":
            row.allowed_user_ids = [str(capacity.owner), str(capacity.other)]
        elif damage == "no_allowed_user":
            row.allowed_user_ids = []
        elif damage == "inactive_user":
            session.get(UserAccount, capacity.owner).active = False
        elif damage == "no_snapshot":
            evidence.pop("limits")
        elif damage in {"string_value", "fraction", "negative", "over_ceiling", "missing_key"}:
            limits = dict(evidence["limits"])
            if damage == "missing_key":
                limits.pop("proposal_pending")
            else:
                limits["context_hourly"] = {"string_value": "100", "fraction": 1.5, "negative": -1, "over_ceiling": 101}[damage]
            evidence["limits"] = limits
        elif damage == "bad_grant":
            evidence["grant_id"] = "not-a-uuid"
        else:
            evidence.pop("configuration_fingerprint")
        row.evidence = evidence
        session.flush()
        assert effective(session, capacity.owner) == LOW


def test_personal_context_active_limit_is_30_with_exact_competing_admissions(capacity):
    db = capacity.database
    with db.transaction() as session:
        for _ in range(28):
            ctx = context(session, capacity.owner)
            assert timedelta(hours=23, minutes=59) < ctx.expires_at - ctx.created_at <= timedelta(hours=24)
    def attempt(_):
        try:
            with db.transaction() as session:
                context(session, capacity.owner)
            return True
        except DBAPIError:
            return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(attempt, range(6))) == 2
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AgentContext)) == 30
    with pytest.raises(DBAPIError), db.transaction() as session:
        context(session, capacity.owner)


def test_personal_context_hourly_history_keeps_limit_100_after_expiry(capacity):
    db = capacity.database
    with db.transaction() as session:
        # Passage of time is simulated only in this owned random test database.
        # No public API can mutate immutable contexts or erase hourly history.
        session.execute(text("ALTER TABLE agent_contexts DISABLE TRIGGER context_guard"))
        for _ in range(100):
            row = context(session, capacity.owner)
            session.execute(text("UPDATE agent_contexts SET expires_at=clock_timestamp()-interval '1 second' WHERE id=:id"), {"id": row.id})
        session.execute(text("ALTER TABLE agent_contexts ENABLE TRIGGER context_guard"))
    with pytest.raises(DBAPIError), db.transaction() as session:
        context(session, capacity.owner)
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AgentContext)) == 100


def test_personal_proposal_pending_limit_is_30_with_concurrent_admissions(capacity):
    db = capacity.database
    with db.transaction() as session:
        context_id = context(session, capacity.owner).id
        for _ in range(28):
            row = proposal(session, capacity.owner, context_id)
            assert timedelta(minutes=14, seconds=59) < row.expires_at - row.created_at <= timedelta(minutes=15)
    def attempt(_):
        try:
            with db.transaction() as session:
                proposal(session, capacity.owner, context_id)
            return True
        except DBAPIError:
            return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(attempt, range(6))) == 2
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 30
        assert session.scalar(select(func.count()).select_from(Ticket)) == 0


def test_personal_proposal_daily_history_keeps_limit_200_after_rejection(capacity):
    db = capacity.database
    with db.transaction() as session:
        context_id = context(session, capacity.owner).id
        for _ in range(200):
            row = proposal(session, capacity.owner, context_id)
            session.execute(text("UPDATE pending_actions SET status='cancelled' WHERE id=:id"), {"id": row.id})
    with pytest.raises(DBAPIError), db.transaction() as session:
        proposal(session, capacity.owner, context_id)
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 200
        assert session.scalar(select(func.count()).select_from(Ticket)) == 0


def test_lower_personal_snapshot_is_applied_without_changing_other_owner_policy(capacity):
    db = capacity.database
    with db.transaction() as session:
        grant(session, capacity.owner, limits={name: 2 for name in CAPACITY})
        assert effective(session, capacity.owner) == [2, 2, 2, 2]
        assert effective(session, capacity.other) == LOW
        context_id = context(session, capacity.owner).id
        context(session, capacity.owner)
        proposal(session, capacity.owner, context_id)
        proposal(session, capacity.owner, context_id)
    with pytest.raises(DBAPIError), db.transaction() as session:
        context(session, capacity.owner)
    with pytest.raises(DBAPIError), db.transaction() as session:
        proposal(session, capacity.owner, context_id)


def test_context_transaction_holds_release_admission_until_commit(capacity):
    db = capacity.database
    with db.transaction() as session:
        context(session, capacity.owner)
        with db.transaction() as closing:
            assert closing.scalar(text("SELECT pg_try_advisory_xact_lock(604116220001)")) is False
    with db.transaction() as closing:
        assert closing.scalar(text("SELECT pg_try_advisory_xact_lock(604116220001)")) is True
    with db.transaction() as session:
        row = session.get(ReleaseControl, 1)
        row.state, row.worker_enabled = "CLOSED", False
    with db.transaction() as session:
        assert effective(session, capacity.owner) == LOW
        assert session.scalar(select(func.count()).select_from(AgentContext)) == 1
