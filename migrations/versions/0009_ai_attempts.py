"""Auditable authenticated AI submission outcomes without user payloads."""
from alembic import op
import sqlalchemy as sa

revision = "0009_ai_attempts"
down_revision = "0008_ai_upload_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_request_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), sa.ForeignKey("ai_request_reservations.id")),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("kind IN ('agent', 'rag', 'upload')", name="ck_ai_attempt_kind"),
        sa.CheckConstraint("outcome IN ('accepted', 'reused', 'quota_rejected', 'failed')", name="ck_ai_attempt_outcome"),
        sa.CheckConstraint("(outcome IN ('accepted', 'reused')) = (reservation_id IS NOT NULL)",
                           name="ck_ai_attempt_reservation"),
    )
    op.create_index("ix_ai_attempt_user_created", "ai_request_attempts", ["user_id", "created_at"])
    op.create_index("ix_ai_attempt_created", "ai_request_attempts", ["created_at"])
    op.create_table(
        "ai_audit_rate_buckets",
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("bucket_key", sa.String(64), nullable=False),
        sa.Column("window_started", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint("scope IN ('global', 'account')", name="ck_ai_audit_rate_scope"),
        sa.CheckConstraint("attempts >= 1", name="ck_ai_audit_rate_attempts"),
        sa.PrimaryKeyConstraint("scope", "bucket_key"),
    )
    op.create_index("ix_ai_audit_rate_window", "ai_audit_rate_buckets", ["window_started"])


def downgrade() -> None:
    raise RuntimeError("AI request audit data cannot be removed by automatic downgrade")
