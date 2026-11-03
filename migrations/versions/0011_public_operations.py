"""Bound public proposals and contexts, preserve operational recovery evidence."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011_public_operations"
down_revision = "0010_release_control"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("job_content_tombstones",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("ai_jobs.id"), primary_key=True),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")))
    op.create_table("job_recovery_audits",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("ai_jobs.id"), nullable=False, unique=True),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("previous_state", sa.String(10), nullable=False),
        sa.Column("resolution", sa.String(30), nullable=False),
        sa.Column("provider_attempts", sa.Integer(), nullable=False),
        sa.Column("prior_usage", JSONB()),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("inspection_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("previous_state IN ('running', 'unknown')", name="ck_recovery_previous"),
        sa.CheckConstraint("resolution = 'terminal_no_retry'", name="ck_recovery_resolution"),
        sa.CheckConstraint("evidence_sha256 ~ '^[0-9a-f]{64}$'", name="ck_recovery_evidence"),
        sa.CheckConstraint("inspection_sha256 ~ '^[0-9a-f]{64}$'", name="ck_recovery_inspection"))
    op.create_table("maintenance_audits",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("counts", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")))
    op.create_index("ix_maintenance_audits_created", "maintenance_audits", ["created_at"])
    op.create_index("ix_agent_contexts_owner_created", "agent_contexts", ["owner_user_id", "created_at"])
    op.create_index("ix_pending_actions_owner_created", "pending_actions", ["owner_user_id", "created_at"])
    # The same transaction-scoped user lock serializes context and proposal
    # admission across API instances and Agent tools. Database time is sampled
    # after the wait so hourly/day boundaries cannot use a stale timestamp.
    op.execute("""CREATE FUNCTION guard_public_context_limit() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE wall timestamptz;
    BEGIN
      IF NEW.owner_user_id IS NULL THEN RETURN NEW; END IF;
      PERFORM set_config('lock_timeout', '5s', true);
      PERFORM pg_advisory_xact_lock(hashtextextended('pc-public-operations:' || NEW.owner_user_id::text, 0));
      wall := clock_timestamp();
      IF (SELECT count(*) FROM agent_contexts WHERE owner_user_id=NEW.owner_user_id AND created_at >= wall - interval '1 hour') >= 5
        THEN RAISE EXCEPTION 'public_operation_limit:context_hourly'; END IF;
      IF (SELECT count(*) FROM agent_contexts WHERE owner_user_id=NEW.owner_user_id AND expires_at > wall) >= 3
        THEN RAISE EXCEPTION 'public_operation_limit:context_active'; END IF;
      NEW.created_at := wall;
      NEW.expires_at := wall + interval '24 hours';
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER public_context_limit BEFORE INSERT ON agent_contexts FOR EACH ROW EXECUTE FUNCTION guard_public_context_limit()")
    op.execute("""CREATE FUNCTION guard_public_action_limit() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE wall timestamptz; context_expiry timestamptz;
    BEGIN
      IF NEW.owner_user_id IS NULL THEN RETURN NEW; END IF;
      PERFORM set_config('lock_timeout', '5s', true);
      PERFORM pg_advisory_xact_lock(hashtextextended('pc-public-operations:' || NEW.owner_user_id::text, 0));
      wall := clock_timestamp();
      SELECT expires_at INTO context_expiry FROM agent_contexts WHERE id=NEW.context_id AND owner_user_id=NEW.owner_user_id;
      IF context_expiry IS NULL OR context_expiry <= wall THEN RAISE EXCEPTION 'public_context_expired'; END IF;
      IF (SELECT count(*) FROM pending_actions WHERE owner_user_id=NEW.owner_user_id AND created_at >= (date_trunc('day', wall AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')) >= 10
        THEN RAISE EXCEPTION 'public_operation_limit:proposal_daily'; END IF;
      IF (SELECT count(*) FROM pending_actions WHERE owner_user_id=NEW.owner_user_id AND status='pending' AND expires_at > wall) >= 3
        THEN RAISE EXCEPTION 'public_operation_limit:proposal_pending'; END IF;
      NEW.created_at := wall;
      NEW.expires_at := LEAST(wall + interval '15 minutes', context_expiry);
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER public_action_limit BEFORE INSERT ON pending_actions FOR EACH ROW EXECUTE FUNCTION guard_public_action_limit()")
    op.execute("""CREATE FUNCTION guard_recovery_audit() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'immutable recovery audit'; END $$""")
    op.execute("CREATE TRIGGER recovery_audit_immutable BEFORE UPDATE OR DELETE ON job_recovery_audits FOR EACH ROW EXECUTE FUNCTION guard_recovery_audit()")
    op.execute("CREATE TRIGGER maintenance_audit_immutable BEFORE UPDATE OR DELETE ON maintenance_audits FOR EACH ROW EXECUTE FUNCTION guard_recovery_audit()")


def downgrade():
    raise RuntimeError("Operational evidence and admission controls cannot be removed automatically")
