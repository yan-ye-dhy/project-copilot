"""Durable request payloads. Raw session and context capabilities are never stored."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from project_copilot.db.models import Base


class AiJob(Base):
    __tablename__ = "ai_jobs"

    id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("ai_request_reservations.id"), primary_key=True)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    login_session_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("login_sessions.id"), nullable=False)
    context_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("agent_contexts.id"), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    failure_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class AiJobUpload(Base):
    """Short-lived, bounded attachment. Never include its bytes in API output/logs."""

    __tablename__ = "ai_job_uploads"
    __table_args__ = (
        CheckConstraint("octet_length(content) BETWEEN 1 AND 4194304", name="ck_ai_upload_content_size"),
        CheckConstraint("length(source_name) BETWEEN 1 AND 200", name="ck_ai_upload_source_name"),
        CheckConstraint("length(media_type) BETWEEN 1 AND 127", name="ck_ai_upload_media_type"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_ai_upload_sha256"),
    )

    job_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("ai_jobs.id"), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    source_name: Mapped[str] = mapped_column(String(200), nullable=False)
    media_type: Mapped[str] = mapped_column(String(127), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class AiRequestAttempt(Base):
    """Sanitized authenticated submission outcome; never stores request payloads."""

    __tablename__ = "ai_request_attempts"
    __table_args__ = (
        CheckConstraint("kind IN ('agent', 'rag', 'upload')", name="ck_ai_attempt_kind"),
        CheckConstraint("outcome IN ('accepted', 'reused', 'quota_rejected', 'failed')", name="ck_ai_attempt_outcome"),
        CheckConstraint("(outcome IN ('accepted', 'reused')) = (reservation_id IS NOT NULL)",
                        name="ck_ai_attempt_reservation"),
        Index("ix_ai_attempt_user_created", "user_id", "created_at"),
        Index("ix_ai_attempt_created", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("user_accounts.id"), nullable=False)
    reservation_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("ai_request_reservations.id"))
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                  server_default=text("clock_timestamp()"))


class AiAuditRateBucket(Base):
    """Shared fixed-window limiter; account keys are UUIDs, never credentials."""

    __tablename__ = "ai_audit_rate_buckets"
    __table_args__ = (
        CheckConstraint("scope IN ('global', 'account')", name="ck_ai_audit_rate_scope"),
        CheckConstraint("attempts >= 1", name="ck_ai_audit_rate_attempts"),
        Index("ix_ai_audit_rate_window", "window_started"),
    )

    scope: Mapped[str] = mapped_column(String(8), primary_key=True)
    bucket_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(nullable=False)


class AiJobEvent(Base):
    """Owner-authenticated, bounded content journal, not raw provider payloads."""
    __tablename__ = "ai_job_events"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_job_event_sequence"),
        CheckConstraint("kind IN ('progress','delta','terminal','diagnostic')", name="ck_job_event_kind"),
        CheckConstraint("octet_length(data::text) <= 8192", name="ck_job_event_size"),
        Index("ix_job_events_created", "created_at"),
    )
    job_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("ai_jobs.id"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                               server_default=text("clock_timestamp()"))
