"""Opt-in recovery metadata; historical attempts and business keys are immutable."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from project_copilot.db.models import Base


class AiJobRecoveryState(Base):
    __tablename__ = "ai_job_recovery_states"
    __table_args__ = (
        CheckConstraint("protocol_version = 2", name="ck_job_recovery_protocol"),
        CheckConstraint("current_attempt_no BETWEEN 1 AND 2", name="ck_job_recovery_attempt"),
        CheckConstraint("model_calls >= 0 AND tool_calls >= 0", name="ck_job_recovery_counts"),
        CheckConstraint("max_model_calls > 0 AND max_tool_calls > 0 AND max_provider_attempts > 0 AND timeout_seconds > 0",
                        name="ck_job_recovery_limits"),
        CheckConstraint("attempt_provider_baseline >= 0 AND event_floor >= 0", name="ck_job_recovery_baseline"),
        CheckConstraint("(budget_started_at IS NULL) = (deadline_at IS NULL)", name="ck_job_recovery_deadline"),
        CheckConstraint("deadline_at IS NULL OR deadline_at > budget_started_at", name="ck_job_recovery_deadline_order"),
    )
    job_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("ai_jobs.id"), primary_key=True)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    current_attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    budget_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_model_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    max_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    max_provider_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_provider_baseline: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    event_floor: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    blocked_reason: Mapped[str | None] = mapped_column(String(40))
    last_inspected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                server_default=text("clock_timestamp()"))


class AiJobExecutionAttempt(Base):
    """Complete physical proof snapshot, appended before its current pointer rotates."""
    __tablename__ = "ai_job_execution_attempts"
    __table_args__ = (
        UniqueConstraint("token", name="uq_job_attempt_token"),
        CheckConstraint("attempt_no BETWEEN 1 AND 2", name="ck_job_attempt_number"),
        CheckConstraint("lease_inode > 0 AND provider_open >= 0", name="ck_job_attempt_proof"),
        CheckConstraint("provider_attempts >= provider_baseline AND provider_baseline >= 0", name="ck_job_attempt_provider"),
        CheckConstraint("previous_state IN ('running','unknown','succeeded','failed')", name="ck_job_attempt_state"),
        CheckConstraint("model_calls >= 0 AND tool_calls >= 0", name="ck_job_attempt_counts"),
        CheckConstraint("reason IN ('no_provider','checkpoint','model_repeat')", name="ck_job_attempt_reason"),
    )
    job_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("ai_jobs.id"), primary_key=True)
    attempt_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    lease_inode: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_open: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_uncertain: Mapped[bool] = mapped_column(Boolean, nullable=False)
    provider_baseline: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    action_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("pending_actions.id"))
    local_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fenced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    release_reason: Mapped[str] = mapped_column(String(24), nullable=False)
    execution_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    previous_state: Mapped[str] = mapped_column(String(10), nullable=False)
    usage: Mapped[dict | None] = mapped_column(JSONB)
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=text("clock_timestamp()"))


class JobBusinessOperation(Base):
    """A proposal's identity belongs to the logical job, across worker attempts."""
    __tablename__ = "job_business_operations"
    __table_args__ = (
        UniqueConstraint("action_id", name="uq_job_business_action"),
        CheckConstraint("operation_key = 'propose_ticket'", name="ck_job_business_operation"),
        CheckConstraint("payload_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_job_business_fingerprint"),
    )
    job_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("ai_jobs.id"), primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    action_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("pending_actions.id"), nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                server_default=text("clock_timestamp()"))


class AiJobStep(Base):
    """A bounded normalized operation result; never contains provider headers."""
    __tablename__ = "ai_job_steps"
    __table_args__ = (
        CheckConstraint("length(step_key) BETWEEN 1 AND 128", name="ck_job_step_key"),
        CheckConstraint("kind IN ('model','tool','node')", name="ck_job_step_kind"),
        CheckConstraint("status IN ('started','completed','unknown')", name="ck_job_step_status"),
        CheckConstraint("attempt_no BETWEEN 1 AND 2", name="ck_job_step_attempt"),
        CheckConstraint("input_sha256 ~ '^[0-9a-f]{64}$' AND config_sha256 ~ '^[0-9a-f]{64}$'", name="ck_job_step_fingerprints"),
        CheckConstraint("index_fingerprint IS NULL OR index_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_job_step_index"),
        CheckConstraint("result IS NULL OR octet_length(result::text) <= 262144", name="ck_job_step_size"),
        CheckConstraint("prior_intent IS NULL OR octet_length(prior_intent::text) <= 4096", name="ck_job_step_prior_size"),
        CheckConstraint("result_sha256 IS NULL OR result_sha256 ~ '^[0-9a-f]{64}$'", name="ck_job_step_result_hash"),
        CheckConstraint("(status = 'completed') = (result IS NOT NULL AND result_sha256 IS NOT NULL AND completed_at IS NOT NULL)",
                        name="ck_job_step_completed"),
        CheckConstraint("status = 'completed' OR (result IS NULL AND result_sha256 IS NULL AND completed_at IS NULL)",
                        name="ck_job_step_pending"),
    )
    job_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("ai_jobs.id"), primary_key=True)
    step_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    index_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_token: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB)
    prior_intent: Mapped[dict | None] = mapped_column(JSONB)
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                server_default=text("clock_timestamp()"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
