"""Index the existing durable queue and unreleased execution evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0016_queue_backpressure"
down_revision = "0015_personal_capacity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_ai_reserved_claim", "ai_request_reservations", ["created_at", "id"],
                    postgresql_where=sa.text("state = 'reserved'"))
    op.create_index("ix_ai_held_execution_slots", "ai_request_reservations", ["state"],
                    postgresql_where=sa.text("slot_released_at IS NULL"))


def downgrade():
    op.drop_index("ix_ai_held_execution_slots", table_name="ai_request_reservations")
    op.drop_index("ix_ai_reserved_claim", table_name="ai_request_reservations")
