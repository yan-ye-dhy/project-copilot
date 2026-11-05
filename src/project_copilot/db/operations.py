"""Operational recovery records contain hashes and decisions, never raw evidence."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from project_copilot.db.models import Base


class JobRecoveryAudit(Base):
    __tablename__ = "job_recovery_audits"
    __table_args__ = (
        CheckConstraint("previous_state IN ('running', 'unknown', 'succeeded', 'failed')", name="ck_recovery_previous"),
        CheckConstraint("resolution = 'terminal_no_retry'", name="ck_recovery_resolution"),
        CheckConstraint("evidence_sha256 ~ '^[0-9a-f]{64}$'", name="ck_recovery_evidence"),
        CheckConstraint("inspection_sha256 ~ '^[0-9a-f]{64}$'", name="ck_recovery_inspection"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    job_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("ai_jobs.id"), nullable=False, unique=True)
    actor_user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("user_accounts.id"), nullable=False)
    previous_state: Mapped[str] = mapped_column(String(10), nullable=False)
    resolution: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    prior_usage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    inspection_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                               server_default=text("clock_timestamp()"))


class MaintenanceAudit(Base):
    __tablename__ = "maintenance_audits"
    __table_args__ = (Index("ix_maintenance_audits_created", "created_at"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    actor_user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("user_accounts.id"), nullable=False)
    counts: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                               server_default=text("clock_timestamp()"))


class JobContentTombstone(Base):
    """Deleting private content must never delete its idempotency identity."""
    __tablename__ = "job_content_tombstones"
    job_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("ai_jobs.id"), primary_key=True)
    cleared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                               server_default=text("clock_timestamp()"))
