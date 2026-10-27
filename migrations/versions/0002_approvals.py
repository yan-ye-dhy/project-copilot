"""Add immutable approval proposals and a ticket idempotency key."""
from alembic import op
import sqlalchemy as sa

revision = "0002_approvals"
down_revision = "0001_tickets"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("tickets", sa.Column("creation_key", sa.Uuid(), nullable=True))
    op.create_unique_constraint("uq_tickets_creation_key", "tickets", ["creation_key"])
    op.create_table("agent_contexts",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")))
    op.create_table("pending_actions",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("context_id", sa.Uuid(), sa.ForeignKey("agent_contexts.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("ticket_id", sa.Uuid(), sa.ForeignKey("tickets.id"), unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'completed', 'cancelled', 'expired')", name="ck_action_status"),
        sa.CheckConstraint("(status = 'completed') = (ticket_id IS NOT NULL)", name="ck_action_ticket_state"),
        sa.CheckConstraint("length(title) > 0 AND length(description) BETWEEN 1 AND 10000", name="ck_action_payload"),
        sa.CheckConstraint("expires_at > created_at", name="ck_action_expiry"))
    op.execute("""CREATE FUNCTION guard_action() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' OR NEW.ticket_id IS NOT NULL THEN RAISE EXCEPTION 'invalid initial action'; END IF;
      ELSE
        IF ROW(NEW.id, NEW.context_id, NEW.title, NEW.description, NEW.payload_fingerprint, NEW.created_at, NEW.expires_at) IS DISTINCT FROM ROW(OLD.id, OLD.context_id, OLD.title, OLD.description, OLD.payload_fingerprint, OLD.created_at, OLD.expires_at) THEN RAISE EXCEPTION 'immutable action'; END IF;
        IF OLD.status <> 'pending' AND ROW(NEW.status, NEW.ticket_id) IS DISTINCT FROM ROW(OLD.status, OLD.ticket_id) THEN RAISE EXCEPTION 'terminal action'; END IF;
      END IF;
      IF NEW.status = 'completed' AND NOT EXISTS (SELECT 1 FROM tickets WHERE id = NEW.ticket_id AND creation_key = NEW.id) THEN RAISE EXCEPTION 'ticket mismatch'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER action_guard BEFORE INSERT OR UPDATE ON pending_actions FOR EACH ROW EXECUTE FUNCTION guard_action()")
    op.execute("""CREATE FUNCTION guard_context() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable context'; END $$""")
    op.execute("CREATE TRIGGER context_guard BEFORE UPDATE ON agent_contexts FOR EACH ROW EXECUTE FUNCTION guard_context()")

def downgrade():
    op.drop_table("pending_actions")
    op.drop_table("agent_contexts")
    op.execute("DROP FUNCTION guard_action()")
    op.execute("DROP FUNCTION guard_context()")
    op.drop_constraint("uq_tickets_creation_key", "tickets", type_="unique")
    op.drop_column("tickets", "creation_key")
