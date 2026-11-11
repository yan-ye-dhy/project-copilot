"""New-job opt-in recovery with immutable attempt and business operation evidence."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018_job_recovery"
down_revision = "0017_messaging"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("ai_job_recovery_states",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("ai_jobs.id"), primary_key=True),
        sa.Column("protocol_version", sa.Integer(), nullable=False, server_default=sa.text("2")),
        sa.Column("current_attempt_no", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("budget_started_at", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_model_calls", sa.Integer(), nullable=False),
        sa.Column("max_tool_calls", sa.Integer(), nullable=False),
        sa.Column("max_provider_attempts", sa.Integer(), nullable=False),
        sa.Column("attempt_provider_baseline", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("event_floor", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("blocked_reason", sa.String(40)),
        sa.Column("last_inspected_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("protocol_version = 2", name="ck_job_recovery_protocol"),
        sa.CheckConstraint("current_attempt_no BETWEEN 1 AND 2", name="ck_job_recovery_attempt"),
        sa.CheckConstraint("model_calls >= 0 AND tool_calls >= 0", name="ck_job_recovery_counts"),
        sa.CheckConstraint("max_model_calls > 0 AND max_tool_calls > 0 AND max_provider_attempts > 0 AND timeout_seconds > 0", name="ck_job_recovery_limits"),
        sa.CheckConstraint("attempt_provider_baseline >= 0 AND event_floor >= 0", name="ck_job_recovery_baseline"),
        sa.CheckConstraint("(budget_started_at IS NULL) = (deadline_at IS NULL)", name="ck_job_recovery_deadline"),
        sa.CheckConstraint("deadline_at IS NULL OR deadline_at > budget_started_at", name="ck_job_recovery_deadline_order"),
    )
    op.create_table("ai_job_execution_attempts",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("ai_jobs.id"), primary_key=True),
        sa.Column("attempt_no", sa.Integer(), primary_key=True),
        sa.Column("token", sa.Uuid(), nullable=False),
        sa.Column("lease_inode", sa.BigInteger(), nullable=False),
        sa.Column("provider_open", sa.Integer(), nullable=False),
        sa.Column("provider_uncertain", sa.Boolean(), nullable=False),
        sa.Column("provider_baseline", sa.Integer(), nullable=False),
        sa.Column("provider_attempts", sa.Integer(), nullable=False),
        sa.Column("action_id", sa.Uuid(), sa.ForeignKey("pending_actions.id")),
        sa.Column("local_done_at", sa.DateTime(timezone=True)),
        sa.Column("fenced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("release_reason", sa.String(24), nullable=False),
        sa.Column("execution_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_state", sa.String(10), nullable=False),
        sa.Column("usage", JSONB),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.UniqueConstraint("token", name="uq_job_attempt_token"),
        sa.CheckConstraint("attempt_no BETWEEN 1 AND 2", name="ck_job_attempt_number"),
        sa.CheckConstraint("lease_inode > 0 AND provider_open >= 0", name="ck_job_attempt_proof"),
        sa.CheckConstraint("provider_attempts >= provider_baseline AND provider_baseline >= 0", name="ck_job_attempt_provider"),
        sa.CheckConstraint("previous_state IN ('running','unknown','succeeded','failed')", name="ck_job_attempt_state"),
        sa.CheckConstraint("model_calls >= 0 AND tool_calls >= 0", name="ck_job_attempt_counts"),
        sa.CheckConstraint("reason IN ('no_provider','checkpoint','model_repeat')", name="ck_job_attempt_reason"),
    )
    op.create_table("job_business_operations",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("ai_jobs.id"), primary_key=True),
        sa.Column("operation_key", sa.String(32), primary_key=True),
        sa.Column("action_id", sa.Uuid(), sa.ForeignKey("pending_actions.id"), nullable=False),
        sa.Column("payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.UniqueConstraint("action_id", name="uq_job_business_action"),
        sa.CheckConstraint("operation_key = 'propose_ticket'", name="ck_job_business_operation"),
        sa.CheckConstraint("payload_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_job_business_fingerprint"),
    )
    op.execute("""CREATE FUNCTION pc_job_recovery_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'job recovery evidence is immutable'; END; $$""")
    for table in ("ai_job_execution_attempts", "job_business_operations"):
        op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION pc_job_recovery_evidence_immutable()")
    op.create_table("ai_job_steps",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("ai_jobs.id"), primary_key=True),
        sa.Column("step_key", sa.String(128), primary_key=True),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("index_fingerprint", sa.String(64)),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("execution_token", sa.Uuid(), nullable=False),
        sa.Column("result", JSONB),
        sa.Column("prior_intent", JSONB),
        sa.Column("result_sha256", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("length(step_key) BETWEEN 1 AND 128", name="ck_job_step_key"),
        sa.CheckConstraint("kind IN ('model','tool','node')", name="ck_job_step_kind"),
        sa.CheckConstraint("status IN ('started','completed','unknown')", name="ck_job_step_status"),
        sa.CheckConstraint("attempt_no BETWEEN 1 AND 2", name="ck_job_step_attempt"),
        sa.CheckConstraint("input_sha256 ~ '^[0-9a-f]{64}$' AND config_sha256 ~ '^[0-9a-f]{64}$'", name="ck_job_step_fingerprints"),
        sa.CheckConstraint("index_fingerprint IS NULL OR index_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_job_step_index"),
        sa.CheckConstraint("result IS NULL OR octet_length(result::text) <= 262144", name="ck_job_step_size"),
        sa.CheckConstraint("prior_intent IS NULL OR octet_length(prior_intent::text) <= 4096", name="ck_job_step_prior_size"),
        sa.CheckConstraint("result_sha256 IS NULL OR result_sha256 ~ '^[0-9a-f]{64}$'", name="ck_job_step_result_hash"),
        sa.CheckConstraint("(status = 'completed') = (result IS NOT NULL AND result_sha256 IS NOT NULL AND completed_at IS NOT NULL)", name="ck_job_step_completed"),
        sa.CheckConstraint("status = 'completed' OR (result IS NULL AND result_sha256 IS NULL AND completed_at IS NULL)", name="ck_job_step_pending"),
    )


def downgrade():
    raise RuntimeError("Recovery execution and business identities must not be discarded")
