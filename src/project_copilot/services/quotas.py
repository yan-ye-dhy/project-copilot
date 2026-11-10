"""Atomic durable reservations for logical AI requests, not a billing ledger.

All methods participate in the caller's transaction. Commit a reservation and
its job together before contacting an upstream provider. A process crash leaves
work occupying its slot unless the personal execution protocol proves it ended.
No task is automatically retried.
"""
from datetime import timezone
import re
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from project_copilot.db.models import AiRequestReservation


class QuotaExceeded(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("AI request quota exceeded")


class IdempotencyConflict(ValueError):
    pass


class QuotaStateError(ValueError):
    pass


_KEY = re.compile(r"[A-Za-z0-9_-]{8,128}\Z", re.ASCII)
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_USAGE_KEYS = frozenset({"prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "reasoning_tokens"})


class QuotaService:
    def __init__(self, session: Session, *, user_daily_limit: int = 5, global_daily_limit: int = 30,
                 user_concurrency_limit: int = 1, settings=None):
        limits = (user_daily_limit, global_daily_limit, user_concurrency_limit)
        if any(type(value) is not int or value < 1 or value > 1_000_000 for value in limits):
            raise ValueError("invalid AI quota configuration")
        self.session = session
        self.settings = settings
        self.user_daily_limit, self.global_daily_limit, self.user_concurrency_limit = limits

    def reserve(self, user_id: UUID, idempotency_key: str, payload_fingerprint: str,
                kind: str) -> tuple[AiRequestReservation, bool]:
        if not isinstance(user_id, UUID) or not isinstance(idempotency_key, str) or not _KEY.fullmatch(idempotency_key):
            raise ValueError("invalid reservation identity")
        if not isinstance(payload_fingerprint, str) or not _FINGERPRINT.fullmatch(payload_fingerprint):
            raise ValueError("invalid request fingerprint")
        if kind not in {"agent", "rag", "upload"}:
            raise ValueError("invalid request kind")
        release = None
        if self.settings is not None and (self.settings.public_demo or self.settings.environment == "production"):
            from project_copilot.services.release_control import require_open
            # Take the shared admission lock before the quota lock. A close or
            # expiry between HTTP admission and this transaction cannot reserve
            # fresh paid work, and closing follows the same lock order.
            release = require_open(self.session, self.settings, user_id=user_id)
        # One database-wide lock serializes the global and per-user limits across
        # processes. It is transaction-scoped and cannot outlive rollback/commit.
        self.session.execute(text("SELECT pg_advisory_xact_lock(576572591122)"))
        if release is not None:
            # A lock wait can cross the grant deadline even though its shared
            # admission lease prevents an operator changing the row meanwhile.
            release = require_open(self.session, self.settings, user_id=user_id)
        existing = self.session.scalar(select(AiRequestReservation).where(
            AiRequestReservation.user_id == user_id,
            AiRequestReservation.idempotency_key == idempotency_key,
        ))
        if existing is not None:
            if existing.payload_fingerprint != payload_fingerprint or existing.kind != kind:
                raise IdempotencyConflict("idempotency key was used for another request")
            if release is not None:
                require_open(self.session, self.settings, user_id=user_id, reservation=existing)
            return existing, True

        if self.settings is not None:
            from project_copilot.services.redis_admission import admit
            # Replayed requests are resolved above. Redis is an additional rate
            # gate, never a replacement for the durable quota transaction.
            admit(self.settings, user_id, scope="jobs")
            from project_copilot.services.queue_control import check_enqueue
            check_enqueue(self.session, self.settings)

        # Read wall-clock time *after* the lock wait; PostgreSQL now() is fixed at
        # transaction start and would give the wrong day around UTC midnight.
        now = self.session.scalar(select(func.clock_timestamp()))
        day = now.astimezone(timezone.utc).date()
        count = lambda *criteria: self.session.scalar(select(func.count()).select_from(AiRequestReservation).where(*criteria))
        personal_grant = UUID(release.evidence["grant_id"]) if release is not None and release.state == "PERSONAL_DEMO" else None
        from project_copilot.services.personal_profile import PERSONAL_JOB_LIMIT, personal_limits
        profile = personal_limits(self.settings) if personal_grant is not None else None
        if personal_grant is not None and count(AiRequestReservation.personal_demo_grant_id == personal_grant) >= PERSONAL_JOB_LIMIT:
            # Count all outcomes, across UTC midnight. Failure, timeout or a new
            # idempotency key cannot refund this grant's tasks.
            raise QuotaExceeded("personal_demo_limit")
        concurrency_limit = profile["concurrent_limit"] if profile else self.user_concurrency_limit
        user_daily_limit = profile["user_daily_limit"] if profile else self.user_daily_limit
        global_daily_limit = profile["global_daily_limit"] if profile else self.global_daily_limit
        if personal_grant is not None:
            from project_copilot.services.execution_slots import reclaim_ended
            # Proof-based maintenance only; this never reruns work or refunds a task.
            reclaim_ended(self.session, self.settings, user_id=user_id)
        if count(AiRequestReservation.user_id == user_id, AiRequestReservation.slot_released_at.is_(None)) >= concurrency_limit:
            raise QuotaExceeded("user_concurrency")
        if count(AiRequestReservation.user_id == user_id, AiRequestReservation.utc_day == day) >= user_daily_limit:
            raise QuotaExceeded("user_daily")
        if count(AiRequestReservation.utc_day == day) >= global_daily_limit:
            raise QuotaExceeded("global_daily")
        reservation = AiRequestReservation(user_id=user_id, idempotency_key=idempotency_key,
                                           payload_fingerprint=payload_fingerprint, kind=kind,
                                           utc_day=day, created_at=now, state="reserved",
                                           personal_demo_grant_id=personal_grant)
        self.session.add(reservation)
        self.session.flush()
        return reservation, False

    def claim(self, user_id: UUID, reservation_id: UUID) -> bool:
        """Claim once; neither duplicate submissions nor crashes restart work."""
        result = self.session.execute(update(AiRequestReservation).where(
            AiRequestReservation.id == reservation_id, AiRequestReservation.user_id == user_id,
            AiRequestReservation.state == "reserved",
        ).values(state="running").returning(AiRequestReservation.id))
        return result.scalar_one_or_none() is not None

    def _running(self, user_id: UUID, reservation_id: UUID) -> AiRequestReservation:
        row = self.session.scalar(select(AiRequestReservation).where(
            AiRequestReservation.id == reservation_id, AiRequestReservation.user_id == user_id,
        ).with_for_update().execution_options(populate_existing=True))
        if row is None or row.state != "running":
            raise QuotaStateError("reservation is not running")
        return row

    def mark_provider_attempt(self, user_id: UUID, reservation_id: UUID, *, max_attempts: int | None = None) -> int:
        """Record only an actually intercepted upstream attempt, before sending."""
        if max_attempts is not None and (type(max_attempts) is not int or not 1 <= max_attempts <= 1_000_000):
            raise ValueError("invalid provider attempt limit")
        row = self._running(user_id, reservation_id)
        if max_attempts is not None and row.provider_attempts >= max_attempts:
            raise QuotaExceeded("provider_attempts")
        row.provider_attempts += 1
        self.session.flush()
        return row.provider_attempts

    def finish(self, user_id: UUID, reservation_id: UUID, *, outcome: str,
               usage: dict[str, int] | None = None) -> None:
        if outcome not in {"succeeded", "failed", "unknown"}:
            raise ValueError("invalid reservation outcome")
        if usage is not None and (not isinstance(usage, dict) or not set(usage).issubset(_USAGE_KEYS) or
                                  any(type(value) is not int or value < 0 or value > 2**63 - 1 for value in usage.values())):
            raise ValueError("invalid usage")
        row = self._running(user_id, reservation_id)
        row.state = outcome
        row.usage = dict(usage) if usage is not None else None
        row.finished_at = self.session.scalar(select(func.clock_timestamp())) if outcome != "unknown" else None
        from project_copilot.db.executions import AiJobExecution
        # Enrolled personal executions release only after physical drain proof.
        # Non-enrolled terminal work retains the existing success/failure behavior.
        if outcome in {"succeeded", "failed"} and self.session.get(AiJobExecution, row.id) is None:
            row.slot_released_at = row.finished_at
        self.session.flush()
