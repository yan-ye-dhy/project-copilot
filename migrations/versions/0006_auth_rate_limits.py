"""Bound public authentication attempts in PostgreSQL shared windows."""
from alembic import op
import sqlalchemy as sa

revision = "0006_auth_rate_limits"
down_revision = "0005_ai_quotas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_rate_buckets",
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("bucket_key", sa.String(64), nullable=False),
        sa.Column("window_started", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint("scope IN ('global', 'account')", name="ck_auth_rate_scope"),
        sa.CheckConstraint("attempts >= 1", name="ck_auth_rate_attempts"),
        sa.PrimaryKeyConstraint("scope", "bucket_key"),
    )
    op.create_index("ix_auth_rate_buckets_window_started", "auth_rate_buckets", ["window_started"])


def downgrade() -> None:
    raise RuntimeError("Authentication rate history cannot be removed by automatic downgrade")
