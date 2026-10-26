"""工单存储映射；约束由迁移写入PostgreSQL，不在应用启动时create_all。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, Uuid, func, text
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
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'open'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(),
    )
