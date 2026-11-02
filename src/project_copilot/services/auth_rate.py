"""Short PostgreSQL-backed public authentication attempt windows."""

import hashlib
import hmac

from sqlalchemy import text
from sqlalchemy.orm import Session


GLOBAL_PER_MINUTE = 60
ACCOUNT_PER_MINUTE = 5

# The database clock is read *after* the conflicting row is locked, so a wait
# spanning a window boundary cannot reuse a stale application-side timestamp.
_INCREMENT = text("""
    INSERT INTO auth_rate_buckets(scope, bucket_key, window_started, attempts)
    VALUES (:scope, :bucket_key, date_trunc('minute', clock_timestamp()), 1)
    ON CONFLICT (scope, bucket_key) DO UPDATE SET
      attempts = CASE
        WHEN auth_rate_buckets.window_started < date_trunc('minute', clock_timestamp()) THEN 1
        ELSE LEAST(auth_rate_buckets.attempts + 1, 1000000)
      END,
      window_started = GREATEST(auth_rate_buckets.window_started, date_trunc('minute', clock_timestamp()))
    RETURNING attempts
""")


def account_key(secret: str, username: str) -> str:
    """Use a server-only HMAC key so the table does not store account names."""
    if not isinstance(secret, str) or len(secret) < 32 or not secret.isascii():
        raise ValueError("authentication limiter is not configured")
    return hmac.new(secret.encode("ascii"), b"project-copilot-auth-rate-v1\0" + username.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def consume_auth_attempt(session: Session, *, username: str, secret: str) -> bool:
    """Reserve one attempt; callers must commit this transaction even on denial.

    Global is always locked first. Account buckets are only created inside the
    global budget, bounding an attack using arbitrary usernames. This function
    never trusts or stores any browser-provided client address.
    """
    key = account_key(secret, username)
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    global_count = session.execute(_INCREMENT, {"scope": "global", "bucket_key": "global"}).scalar_one()
    if global_count > GLOBAL_PER_MINUTE:
        return False
    # Bounded cleanup is protected by the global row lock; each allowed request
    # removes up to 100 inactive account keys without an unbounded table sweep.
    session.execute(text("""
        DELETE FROM auth_rate_buckets WHERE ctid IN (
          SELECT ctid FROM auth_rate_buckets
          WHERE scope = 'account' AND window_started < clock_timestamp() - interval '1 day'
          LIMIT 100
        )
    """))
    count = session.execute(_INCREMENT, {"scope": "account", "bucket_key": key}).scalar_one()
    return count <= ACCOUNT_PER_MINUTE
