"""Link indexing jobs to immutable document revisions."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261003_02"
down_revision: str | None = "20261003_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("indexing_jobs", sa.Column("revision_id", sa.String(length=36), nullable=True))
    op.create_index("ix_indexing_jobs_revision", "indexing_jobs", ["revision_id"])


def downgrade() -> None:
    op.drop_index("ix_indexing_jobs_revision", table_name="indexing_jobs")
    op.drop_column("indexing_jobs", "revision_id")
