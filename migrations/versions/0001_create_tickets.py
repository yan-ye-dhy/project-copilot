"""Create the ticket table and database-enforced constraints."""

from alembic import op
import sqlalchemy as sa


revision = "0001_tickets"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'open'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("title ~ '[^[:space:]]'", name="ck_tickets_title_not_blank"),
        sa.CheckConstraint("description ~ '[^[:space:]]'", name="ck_tickets_description_not_blank"),
        sa.CheckConstraint("length(description) <= 10000", name="ck_tickets_description_length"),
        sa.CheckConstraint("status IN ('open', 'in_progress', 'closed')", name="ck_tickets_status"),
    )
    op.create_index("ix_tickets_created_id", "tickets", ["created_at", "id"])
    op.create_index("ix_tickets_status_created_id", "tickets", ["status", "created_at", "id"])


def downgrade() -> None:
    # Alembic's explicit downgrade is destructive; never used by app startup/tests.
    op.drop_index("ix_tickets_status_created_id", table_name="tickets")
    op.drop_index("ix_tickets_created_id", table_name="tickets")
    op.drop_table("tickets")
