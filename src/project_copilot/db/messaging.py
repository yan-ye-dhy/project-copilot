"""Broker delivery evidence; request payloads stay exclusively in PostgreSQL jobs."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from project_copilot.db.models import Base


class AiJobOutbox(Base):
    __tablename__ = "ai_job_outbox"
    __table_args__ = (
        UniqueConstraint("job_id", "dispatch_generation", name="uq_job_outbox_generation"),
        CheckConstraint("dispatch_generation > 0", name="ck_job_outbox_generation"),
        CheckConstraint("schema_version = 1", name="ck_job_outbox_schema"),
        CheckConstraint("publish_attempts >= 0", name="ck_job_outbox_attempts"),
        CheckConstraint("(lease_token IS NULL) = (lease_until IS NULL)", name="ck_job_outbox_lease"),
        CheckConstraint("sent_at IS NULL OR lease_token IS NULL", name="ck_job_outbox_sent_lease"),
        CheckConstraint("failure_code IS NULL OR failure_code = 'publish_failed'", name="ck_job_outbox_failure"),
        Index("ix_job_outbox_due", "available_at", "event_id", postgresql_where=text("sent_at IS NULL")),
    )
    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    job_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("ai_jobs.id"), nullable=False)
    dispatch_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    failure_code: Mapped[str | None] = mapped_column(String(20))


class AiJobInbox(Base):
    __tablename__ = "ai_job_inbox"
    __table_args__ = (
        CheckConstraint("dispatch_generation > 0", name="ck_job_inbox_generation"),
        CheckConstraint("state IN ('processing','deferred','completed')", name="ck_job_inbox_state"),
        CheckConstraint("deliveries > 0", name="ck_job_inbox_deliveries"),
        CheckConstraint("(lease_token IS NULL) = (lease_until IS NULL)", name="ck_job_inbox_lease"),
        CheckConstraint("(state = 'processing') = (lease_token IS NOT NULL)", name="ck_job_inbox_processing"),
        CheckConstraint("(state = 'completed') = (completed_at IS NOT NULL)", name="ck_job_inbox_completed"),
        CheckConstraint("outcome IS NULL OR outcome IN ('succeeded','failed','unknown')", name="ck_job_inbox_outcome"),
        CheckConstraint("(state = 'completed') = (outcome IS NOT NULL)", name="ck_job_inbox_outcome_state"),
        CheckConstraint("defer_reason IS NULL OR defer_reason IN ('capacity','active','release','unavailable')", name="ck_job_inbox_defer"),
        Index("ix_job_inbox_job", "job_id", "dispatch_generation"),
    )
    event_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("ai_job_outbox.event_id"), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("ai_jobs.id"), nullable=False)
    dispatch_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(12), nullable=False)
    deliveries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    first_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    last_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(10))
    defer_reason: Mapped[str | None] = mapped_column(String(12))
