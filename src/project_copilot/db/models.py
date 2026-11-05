"""工单存储映射；约束由迁移写入PostgreSQL，不在应用启动时create_all。"""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint("title ~ '[^[:space:]]'", name="ck_tickets_title_not_blank"),
        CheckConstraint("description ~ '[^[:space:]]'", name="ck_tickets_description_not_blank"),
        CheckConstraint("length(description) <= 10000", name="ck_tickets_description_length"),
        CheckConstraint("status IN ('open', 'in_progress', 'closed')", name="ck_tickets_status"),
        Index("ix_tickets_created_id", "created_at", "id"),
        Index("ix_tickets_status_created_id", "status", "created_at", "id"),
        Index("ix_tickets_owner_user_id", "owner_user_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    creation_key: Mapped[UUID | None] = mapped_column(Uuid, nullable=True, unique=True)
    owner_user_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("user_accounts.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'open'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class AgentContext(Base):
    __tablename__ = "agent_contexts"
    __table_args__ = (
        Index("ix_agent_contexts_owner_user_id", "owner_user_id"),
        Index("ix_agent_contexts_owner_created", "owner_user_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    owner_user_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("user_accounts.id"), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PendingAction(Base):
    __tablename__ = "pending_actions"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'completed', 'cancelled', 'expired')", name="ck_action_status"),
        CheckConstraint("(status = 'completed') = (ticket_id IS NOT NULL)", name="ck_action_ticket_state"),
        CheckConstraint("length(title) > 0 AND length(description) BETWEEN 1 AND 10000", name="ck_action_payload"),
        CheckConstraint("expires_at > created_at", name="ck_action_expiry"),
        Index("ix_pending_actions_owner_user_id", "owner_user_id"),
        Index("ix_pending_actions_owner_created", "owner_user_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    context_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("agent_contexts.id"), nullable=False)
    owner_user_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("user_accounts.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    ticket_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("tickets.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserAccount(Base):
    """Authentication identity; never expose the password hash in API schemas."""

    __tablename__ = "user_accounts"
    __table_args__ = (
        CheckConstraint("username ~ '^[a-z0-9][a-z0-9_.-]{2,31}$'", name="ck_user_username"),
        CheckConstraint("role IN ('user', 'admin')", name="ck_user_role"),
        CheckConstraint("password_hash LIKE '$argon2id$%'", name="ck_user_password_argon2id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    username: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'user'"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class LoginSession(Base):
    """Only a SHA-256 digest of the random cookie token is persisted."""

    __tablename__ = "login_sessions"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="ck_login_session_expiry"),
        Index("ix_login_sessions_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("user_accounts.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RegistrationInvite(Base):
    __tablename__ = "registration_invites"
    __table_args__ = (
        CheckConstraint("max_uses > 0 AND uses >= 0 AND uses <= max_uses", name="ck_invite_uses"),
        CheckConstraint("expires_at > created_at", name="ck_invite_expiry"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_by: Mapped[UUID] = mapped_column(Uuid, ForeignKey("user_accounts.id"), nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthRateBucket(Base):
    """Shared fixed-window authentication attempts; account keys are keyed digests."""

    __tablename__ = "auth_rate_buckets"
    __table_args__ = (
        CheckConstraint("scope IN ('global', 'account')", name="ck_auth_rate_scope"),
        CheckConstraint("attempts >= 1", name="ck_auth_rate_attempts"),
        Index("ix_auth_rate_buckets_window_started", "window_started"),
    )

    scope: Mapped[str] = mapped_column(String(8), primary_key=True)
    bucket_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)


class AiRequestReservation(Base):
    """One durable logical request; provider calls and actual billing are separate."""

    __tablename__ = "ai_request_reservations"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_ai_reservation_user_key"),
        CheckConstraint("kind IN ('agent', 'rag', 'upload')", name="ck_ai_reservation_kind"),
        CheckConstraint("state IN ('reserved', 'running', 'unknown', 'succeeded', 'failed')", name="ck_ai_reservation_state"),
        CheckConstraint("(state IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)", name="ck_ai_reservation_finish"),
        CheckConstraint("provider_attempts >= 0", name="ck_ai_reservation_attempts"),
        CheckConstraint("payload_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_ai_reservation_fingerprint"),
        CheckConstraint("cost_status = 'unknown'", name="ck_ai_reservation_cost"),
        Index("ix_ai_reservations_day", "utc_day"),
        Index("ix_ai_reservations_user_day", "user_id", "utc_day"),
        Index("ix_ai_reservations_user_state", "user_id", "state"),
        Index("ix_ai_reservations_personal_grant", "personal_demo_grant_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("user_accounts.id"), nullable=False)
    personal_demo_grant_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    state: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'reserved'"))
    utc_day: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    slot_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    usage: Mapped[dict[str, int] | None] = mapped_column(JSONB, nullable=True)
    cost_status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'unknown'"))
