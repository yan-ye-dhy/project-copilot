"""Distinct short personal grants; never relabel CLOSED or fabricate STAGING."""
from alembic import op
import sqlalchemy as sa

revision = "0012_personal_demo"
down_revision = "0011_public_operations"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_release_state", "release_control", type_="check")
    op.alter_column("release_control", "state", type_=sa.String(16), existing_type=sa.String(8), existing_nullable=False)
    op.alter_column("release_events", "before_state", type_=sa.String(16), existing_type=sa.String(8), existing_nullable=False)
    op.alter_column("release_events", "after_state", type_=sa.String(16), existing_type=sa.String(8), existing_nullable=False)
    op.create_check_constraint("ck_release_state", "release_control", "state IN ('CLOSED','STAGING','PUBLIC','PERSONAL_DEMO')")
    op.add_column("ai_request_reservations", sa.Column("personal_demo_grant_id", sa.Uuid(), nullable=True))
    op.create_index("ix_ai_reservations_personal_grant", "ai_request_reservations", ["personal_demo_grant_id"])


def downgrade():
    raise RuntimeError("personal demo grants and audit history must not be truncated")
