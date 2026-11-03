"""Explicit, audited release state; no environment variable can open ingress."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_release_control"
down_revision = "0009_ai_attempts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("release_control",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(8), nullable=False, server_default=sa.text("'CLOSED'")),
        sa.Column("worker_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("resource_id", sa.String(80)), sa.Column("code_fingerprint", sa.String(64)),
        sa.Column("origin", sa.String(255)),
        sa.Column("allowed_user_ids", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("evidence", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("id = 1", name="ck_release_singleton"),
        sa.CheckConstraint("state IN ('CLOSED','STAGING','PUBLIC')", name="ck_release_state"),
        sa.CheckConstraint("state != 'CLOSED' OR NOT worker_enabled", name="ck_release_closed_worker"))
    op.execute("INSERT INTO release_control(id) VALUES (1)")
    op.create_table("release_events",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("operation", sa.String(30), nullable=False),
        sa.Column("before_state", sa.String(8), nullable=False), sa.Column("after_state", sa.String(8), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("evidence_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")))
    op.execute("""CREATE FUNCTION pc_release_event_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'release events are immutable'; END; $$""")
    op.execute("CREATE TRIGGER release_events_immutable BEFORE UPDATE OR DELETE ON release_events FOR EACH ROW EXECUTE FUNCTION pc_release_event_immutable()")


def downgrade():
    raise RuntimeError("release authorization audit cannot be automatically deleted")
