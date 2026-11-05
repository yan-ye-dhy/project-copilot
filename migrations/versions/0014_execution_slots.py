"""Decouple PERSONAL_DEMO slot ownership from uncertain delivery status."""
from alembic import op
import sqlalchemy as sa

revision = "0014_execution_slots"
down_revision = "0013_job_events"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ai_request_reservations", sa.Column("slot_released_at", sa.DateTime(timezone=True), nullable=True))
    # Preserve the old already-terminal admission behavior. Never infer evidence
    # for a historical reserved/running/unknown task.
    op.execute("UPDATE ai_request_reservations SET slot_released_at=finished_at WHERE state IN ('succeeded','failed')")
    # A completed result can still hold its execution slot if the provider's
    # completion acknowledgement failed to commit. Reconcile it without
    # rewriting that successful/failed result to fit the manual audit record.
    op.drop_constraint("ck_recovery_previous", "job_recovery_audits", type_="check")
    op.create_check_constraint("ck_recovery_previous", "job_recovery_audits",
        "previous_state IN ('running', 'unknown', 'succeeded', 'failed')")
    op.create_table("ai_job_executions",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("ai_request_reservations.id"), primary_key=True),
        sa.Column("token", sa.Uuid(), nullable=False, unique=True),
        sa.Column("lease_inode", sa.BigInteger(), nullable=False),
        sa.Column("provider_open", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("provider_uncertain", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("action_id", sa.Uuid(), sa.ForeignKey("pending_actions.id"), nullable=True),
        sa.Column("local_done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fenced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(24), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("lease_inode > 0", name="ck_job_execution_inode"),
        sa.CheckConstraint("provider_open >= 0", name="ck_job_execution_provider_open"),
        sa.CheckConstraint("release_reason IS NULL OR release_reason IN ('drained','dead_readonly','dead_agent_fenced','manual')",
                           name="ck_job_execution_reason"),
        sa.CheckConstraint("(fenced_at IS NULL) = (release_reason IS NULL)", name="ck_job_execution_fence"),
    )


def downgrade():
    raise RuntimeError("Execution evidence and released slot identities must not be discarded")
