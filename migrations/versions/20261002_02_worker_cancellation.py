"""Persist document worker cancellation timestamps."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261002_02"
down_revision: str | None = "20260831_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "indexing_job_documents",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("indexing_job_documents", "cancelled_at")
