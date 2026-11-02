"""Bind business records to accounts without assigning ownership to legacy data."""
from alembic import op
import sqlalchemy as sa

revision = "0004_resource_owners"
down_revision = "0003_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("tickets", "agent_contexts", "pending_actions"):
        op.add_column(table, sa.Column("owner_user_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_owner_user_id_user_accounts", table, "user_accounts", ["owner_user_id"], ["id"]
        )
        op.create_index(f"ix_{table}_owner_user_id", table, ["owner_user_id"])

    # Existing rows remain NULL; IS NOT DISTINCT FROM means legacy context and
    # action records still satisfy the same integrity rule.
    op.execute("""CREATE OR REPLACE FUNCTION guard_action() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' OR NEW.ticket_id IS NOT NULL THEN RAISE EXCEPTION 'invalid initial action'; END IF;
        IF NOT EXISTS (SELECT 1 FROM agent_contexts WHERE id = NEW.context_id AND owner_user_id IS NOT DISTINCT FROM NEW.owner_user_id) THEN RAISE EXCEPTION 'context owner mismatch'; END IF;
      ELSE
        IF ROW(NEW.id, NEW.context_id, NEW.owner_user_id, NEW.title, NEW.description, NEW.payload_fingerprint, NEW.created_at, NEW.expires_at) IS DISTINCT FROM ROW(OLD.id, OLD.context_id, OLD.owner_user_id, OLD.title, OLD.description, OLD.payload_fingerprint, OLD.created_at, OLD.expires_at) THEN RAISE EXCEPTION 'immutable action'; END IF;
        IF OLD.status <> 'pending' AND ROW(NEW.status, NEW.ticket_id) IS DISTINCT FROM ROW(OLD.status, OLD.ticket_id) THEN RAISE EXCEPTION 'terminal action'; END IF;
      END IF;
      IF NEW.status = 'completed' AND NOT EXISTS (SELECT 1 FROM tickets WHERE id = NEW.ticket_id AND creation_key = NEW.id AND owner_user_id IS NOT DISTINCT FROM NEW.owner_user_id) THEN RAISE EXCEPTION 'ticket mismatch'; END IF;
      RETURN NEW;
    END $$""")
    # Prevent a later ticket update from invalidating a completed action's binding.
    op.execute("""CREATE FUNCTION guard_ticket_ownership() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF ROW(NEW.owner_user_id, NEW.creation_key) IS DISTINCT FROM ROW(OLD.owner_user_id, OLD.creation_key) THEN RAISE EXCEPTION 'immutable ticket owner and creation key'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER ticket_owner_guard BEFORE UPDATE ON tickets FOR EACH ROW EXECUTE FUNCTION guard_ticket_ownership()")


def downgrade() -> None:
    raise RuntimeError("Account ownership cannot be removed by an automatic downgrade")
