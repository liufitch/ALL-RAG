"""Track durable cleanup of soft-deleted document assets."""

from alembic import op
import sqlalchemy as sa

revision = "20261116_01"
down_revision = "20261011_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("cleanup_completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("cleanup_last_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_documents_pending_cleanup", "documents", ["cleanup_completed_at", "cleanup_last_attempt_at"],
                    postgresql_where=sa.text("deleted_at IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ix_documents_pending_cleanup", table_name="documents")
    op.drop_column("documents", "cleanup_last_attempt_at")
    op.drop_column("documents", "cleanup_completed_at")
