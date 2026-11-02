"""Bounded authenticated-submission audit, separate from provider billing."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from project_copilot.db.jobs import AiRequestAttempt

GLOBAL_PER_MINUTE = 60
ACCOUNT_PER_MINUTE = 10
RETENTION_DAYS = 7
_INCREMENT = text("""
    INSERT INTO ai_audit_rate_buckets(scope, bucket_key, window_started, attempts)
    VALUES (:scope, :bucket_key, date_trunc('minute', clock_timestamp()), 1)
    ON CONFLICT (scope, bucket_key) DO UPDATE SET
      attempts = CASE
        WHEN ai_audit_rate_buckets.window_started < date_trunc('minute', clock_timestamp()) THEN 1
        ELSE LEAST(ai_audit_rate_buckets.attempts + 1, 1000000)
      END,
      window_started = GREATEST(ai_audit_rate_buckets.window_started, date_trunc('minute', clock_timestamp()))
    RETURNING attempts
""")


class AuditRateExceeded(ValueError):
    """No audit slot: the caller must reject the entire request."""


def record_attempt(session: Session, user_id, kind: str, outcome: str, reservation_id=None) -> None:
    """Atomically bound new rows; denied attempts must not create audit rows.

    Each committed audit also removes bounded batches of older records. No
    activity means no cleanup; rejected bursts still put load on PostgreSQL.
    """
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    global_count = session.execute(_INCREMENT, {"scope": "global", "bucket_key": "global"}).scalar_one()
    if global_count > GLOBAL_PER_MINUTE:
        raise AuditRateExceeded("audit global rate exceeded")
    account_count = session.execute(_INCREMENT, {"scope": "account", "bucket_key": str(user_id)}).scalar_one()
    if account_count > ACCOUNT_PER_MINUTE:
        raise AuditRateExceeded("audit account rate exceeded")
    session.execute(text("""
        DELETE FROM ai_request_attempts WHERE ctid IN (
          SELECT ctid FROM ai_request_attempts
          WHERE created_at < clock_timestamp() - (:days * interval '1 day')
          ORDER BY created_at LIMIT 1000
        )
    """), {"days": RETENTION_DAYS})
    session.execute(text("""
        DELETE FROM ai_audit_rate_buckets WHERE ctid IN (
          SELECT ctid FROM ai_audit_rate_buckets
          WHERE scope = 'account' AND window_started < clock_timestamp() - interval '1 day'
          LIMIT 100
        )
    """))
    session.add(AiRequestAttempt(user_id=user_id, kind=kind, outcome=outcome, reservation_id=reservation_id))
    session.flush()
