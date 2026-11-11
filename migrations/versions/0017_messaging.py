"""Opt-in transactional dispatch and durable consumer delivery decisions."""
from alembic import op
import sqlalchemy as sa

revision = "0017_messaging"
down_revision = "0016_queue_backpressure"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("ai_job_outbox",
        sa.Column("event_id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("ai_jobs.id"), nullable=False),
        sa.Column("dispatch_generation", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_code", sa.String(20)),
        sa.UniqueConstraint("job_id", "dispatch_generation", name="uq_job_outbox_generation"),
        sa.CheckConstraint("dispatch_generation > 0", name="ck_job_outbox_generation"),
        sa.CheckConstraint("schema_version = 1", name="ck_job_outbox_schema"),
        sa.CheckConstraint("publish_attempts >= 0", name="ck_job_outbox_attempts"),
        sa.CheckConstraint("(lease_token IS NULL) = (lease_until IS NULL)", name="ck_job_outbox_lease"),
        sa.CheckConstraint("sent_at IS NULL OR lease_token IS NULL", name="ck_job_outbox_sent_lease"),
        sa.CheckConstraint("failure_code IS NULL OR failure_code = 'publish_failed'", name="ck_job_outbox_failure"),
    )
    op.create_index("ix_job_outbox_due", "ai_job_outbox", ["available_at", "event_id"],
                    postgresql_where=sa.text("sent_at IS NULL"))
    op.create_table("ai_job_inbox",
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("ai_job_outbox.event_id"), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("ai_jobs.id"), nullable=False),
        sa.Column("dispatch_generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("deliveries", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("first_received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(10)),
        sa.Column("defer_reason", sa.String(12)),
        sa.CheckConstraint("dispatch_generation > 0", name="ck_job_inbox_generation"),
        sa.CheckConstraint("state IN ('processing','deferred','completed')", name="ck_job_inbox_state"),
        sa.CheckConstraint("deliveries > 0", name="ck_job_inbox_deliveries"),
        sa.CheckConstraint("(lease_token IS NULL) = (lease_until IS NULL)", name="ck_job_inbox_lease"),
        sa.CheckConstraint("(state = 'processing') = (lease_token IS NOT NULL)", name="ck_job_inbox_processing"),
        sa.CheckConstraint("(state = 'completed') = (completed_at IS NOT NULL)", name="ck_job_inbox_completed"),
        sa.CheckConstraint("outcome IS NULL OR outcome IN ('succeeded','failed','unknown')", name="ck_job_inbox_outcome"),
        sa.CheckConstraint("(state = 'completed') = (outcome IS NOT NULL)", name="ck_job_inbox_outcome_state"),
        sa.CheckConstraint("defer_reason IS NULL OR defer_reason IN ('capacity','active','release','unavailable')", name="ck_job_inbox_defer"),
    )
    op.create_index("ix_job_inbox_job", "ai_job_inbox", ["job_id", "dispatch_generation"])


def downgrade():
    raise RuntimeError("Dispatch and delivery evidence must not be discarded")
