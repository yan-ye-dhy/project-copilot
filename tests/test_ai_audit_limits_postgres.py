"""Audit admission and retention use a real isolated PostgreSQL database."""
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select, text

from project_copilot.db.jobs import AiAuditRateBucket, AiRequestAttempt
from project_copilot.db.models import UserAccount
from project_copilot.services import ai_audit
from test_public_jobs import prepare


def users(database):
    prepare(database)
    with database.transaction() as session:
        return list(session.scalars(select(UserAccount.id).order_by(UserAccount.username)))


def test_parallel_submissions_are_bounded_and_denials_do_not_add_rows(ticket_database, monkeypatch):
    database = ticket_database
    user, _ = users(database)
    monkeypatch.setattr(ai_audit, "ACCOUNT_PER_MINUTE", 3)

    def attempt(_):
        try:
            with database.transaction() as session:
                ai_audit.record_attempt(session, user, "rag", "failed")
            return True
        except ai_audit.AuditRateExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        accepted = list(executor.map(attempt, range(8)))
    assert sum(accepted) == 3
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiRequestAttempt)) == 3
        assert session.get(AiAuditRateBucket, ("account", str(user))).attempts == 3


def test_global_limit_blocks_all_accounts(ticket_database, monkeypatch):
    database = ticket_database
    alice, bob = users(database)
    monkeypatch.setattr(ai_audit, "GLOBAL_PER_MINUTE", 1)
    with database.transaction() as session:
        ai_audit.record_attempt(session, alice, "rag", "failed")
    try:
        with database.transaction() as session:
            ai_audit.record_attempt(session, bob, "rag", "failed")
    except ai_audit.AuditRateExceeded:
        pass
    else:
        raise AssertionError("global limit did not reject the second account")
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiRequestAttempt)) == 1
        assert session.get(AiAuditRateBucket, ("account", str(bob))) is None


def test_retention_and_inactive_buckets_clean_up_on_audit_activity(ticket_database):
    database = ticket_database
    alice, bob = users(database)
    with database.transaction() as session:
        ai_audit.record_attempt(session, alice, "rag", "failed")
        session.execute(text("UPDATE ai_request_attempts SET created_at = clock_timestamp() - interval '8 days'"))
        session.execute(text("UPDATE ai_audit_rate_buckets SET window_started = clock_timestamp() - interval '2 days' WHERE scope = 'account'"))
    with database.transaction() as session:
        ai_audit.record_attempt(session, bob, "rag", "failed")
    with database.transaction() as session:
        rows = list(session.scalars(select(AiRequestAttempt)))
        assert len(rows) == 1 and rows[0].user_id == bob
        assert session.get(AiAuditRateBucket, ("account", str(alice))) is None
