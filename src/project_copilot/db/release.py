"""Durable, default-closed release control and append-only operator events."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from project_copilot.db.models import Base


class ReleaseControl(Base):
    __tablename__ = "release_control"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_release_singleton"),
        CheckConstraint("state IN ('CLOSED','STAGING','PUBLIC','PERSONAL_DEMO')", name="ck_release_state"),
        CheckConstraint("state != 'CLOSED' OR NOT worker_enabled", name="ck_release_closed_worker"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'CLOSED'"))
    worker_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    resource_id: Mapped[str | None] = mapped_column(String(80))
    code_fingerprint: Mapped[str | None] = mapped_column(String(64))
    origin: Mapped[str | None] = mapped_column(String(255))
    allowed_user_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class ReleaseEvent(Base):
    __tablename__ = "release_events"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    actor_user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("user_accounts.id"), nullable=False)
    operation: Mapped[str] = mapped_column(String(30), nullable=False)
    before_state: Mapped[str] = mapped_column(String(16), nullable=False)
    after_state: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
