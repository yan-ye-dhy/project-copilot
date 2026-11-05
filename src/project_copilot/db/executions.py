"""Execution evidence is separate from a task's delivery/billing outcome."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from project_copilot.db.models import Base


class AiJobExecution(Base):
    __tablename__ = "ai_job_executions"
    __table_args__ = (
        CheckConstraint("lease_inode > 0", name="ck_job_execution_inode"),
        CheckConstraint("provider_open >= 0", name="ck_job_execution_provider_open"),
        CheckConstraint("release_reason IS NULL OR release_reason IN ('drained','dead_readonly','dead_agent_fenced','manual')",
                        name="ck_job_execution_reason"),
        CheckConstraint("(fenced_at IS NULL) = (release_reason IS NULL)", name="ck_job_execution_fence"),
    )

    job_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("ai_request_reservations.id"), primary_key=True)
    token: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True)
    lease_inode: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_open: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    provider_uncertain: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    action_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("pending_actions.id"), nullable=True)
    local_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fenced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                               server_default=text("clock_timestamp()"))
