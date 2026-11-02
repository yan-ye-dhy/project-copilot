"""Durable, user-scoped AI request reservations and metering."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_ai_quotas"
down_revision = "0004_resource_owners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_request_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("state", sa.String(10), nullable=False, server_default=sa.text("'reserved'")),
        sa.Column("utc_day", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("usage", postgresql.JSONB(), nullable=True),
        sa.Column("cost_status", sa.String(10), nullable=False, server_default=sa.text("'unknown'")),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_ai_reservation_user_key"),
        sa.CheckConstraint("kind IN ('agent', 'rag')", name="ck_ai_reservation_kind"),
        sa.CheckConstraint("state IN ('reserved', 'running', 'unknown', 'succeeded', 'failed')", name="ck_ai_reservation_state"),
        sa.CheckConstraint("(state IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)", name="ck_ai_reservation_finish"),
        sa.CheckConstraint("provider_attempts >= 0", name="ck_ai_reservation_attempts"),
        sa.CheckConstraint("payload_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_ai_reservation_fingerprint"),
        sa.CheckConstraint("cost_status = 'unknown'", name="ck_ai_reservation_cost"),
    )
    op.create_index("ix_ai_reservations_day", "ai_request_reservations", ["utc_day"])
    op.create_index("ix_ai_reservations_user_day", "ai_request_reservations", ["user_id", "utc_day"])
    op.create_index("ix_ai_reservations_user_state", "ai_request_reservations", ["user_id", "state"])


def downgrade() -> None:
    raise RuntimeError("AI usage and reservation records cannot be dropped automatically")
