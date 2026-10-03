"""Persist immutable document segment revisions."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261003_01"
down_revision: str | None = "20261002_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("segments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("indexing_job_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], name="fk_document_revisions_dataset"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], name="fk_document_revisions_document"),
        sa.ForeignKeyConstraint(["indexing_job_id"], ["indexing_jobs.id"], name="fk_document_revisions_indexing_job"),
        sa.PrimaryKeyConstraint("id", name="pk_document_revisions"),
        sa.UniqueConstraint("document_id", "version", name="uq_document_revisions_document_version"),
    )
    op.create_index("ix_document_revisions_indexing_job", "document_revisions", ["indexing_job_id"])


def downgrade() -> None:
    op.drop_index("ix_document_revisions_indexing_job", table_name="document_revisions")
    op.drop_table("document_revisions")
