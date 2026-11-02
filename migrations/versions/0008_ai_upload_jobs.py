"""Bound temporary upload payloads for durable, quota-protected jobs."""
from alembic import op
import sqlalchemy as sa

revision = "0008_ai_upload_jobs"
down_revision = "0007_ai_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_ai_reservation_kind", "ai_request_reservations", type_="check")
    op.create_check_constraint("ck_ai_reservation_kind", "ai_request_reservations", "kind IN ('agent', 'rag', 'upload')")
    op.create_table(
        "ai_job_uploads",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("ai_jobs.id"), primary_key=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("source_name", sa.String(200), nullable=False),
        sa.Column("media_type", sa.String(127), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint("octet_length(content) BETWEEN 1 AND 4194304", name="ck_ai_upload_content_size"),
        sa.CheckConstraint("length(source_name) BETWEEN 1 AND 200", name="ck_ai_upload_source_name"),
        sa.CheckConstraint("length(media_type) BETWEEN 1 AND 127", name="ck_ai_upload_media_type"),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_ai_upload_sha256"),
    )


def downgrade() -> None:
    raise RuntimeError("AI job uploads and metering cannot be dropped automatically")
