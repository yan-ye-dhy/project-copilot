"""Persist owner-bound task input without storing browser capabilities."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_ai_jobs"
down_revision = "0006_auth_rate_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_jobs",
        sa.Column("id", sa.Uuid(), sa.ForeignKey("ai_request_reservations.id"), primary_key=True),
        sa.Column("login_session_id", sa.Uuid(), sa.ForeignKey("login_sessions.id"), nullable=False),
        sa.Column("context_id", sa.Uuid(), sa.ForeignKey("agent_contexts.id"), nullable=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
    )


def downgrade() -> None:
    raise RuntimeError("AI job records cannot be removed by an automatic downgrade")
