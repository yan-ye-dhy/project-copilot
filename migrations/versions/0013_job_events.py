"""Bounded cross-process progress journal; never a second task queue."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013_job_events"
down_revision = "0012_personal_demo"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ai_jobs", sa.Column("event_sequence", sa.Integer(), nullable=False, server_default="0"))
    op.create_table("ai_job_events",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("ai_jobs.id"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("sequence > 0", name="ck_job_event_sequence"),
        sa.CheckConstraint("kind IN ('progress','delta','terminal','diagnostic')", name="ck_job_event_kind"),
        sa.CheckConstraint("octet_length(data::text) <= 8192", name="ck_job_event_size"),
    )
    op.create_index("ix_job_events_created", "ai_job_events", ["created_at"])


def downgrade():
    raise RuntimeError("Do not discard task execution evidence automatically")
