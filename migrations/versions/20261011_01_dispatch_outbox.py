"""Track durable indexing message publication attempts."""
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa
revision = "20261011_01"
down_revision = "20261003_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column("indexing_job_documents", sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("indexing_job_documents", sa.Column("last_dispatch_error", sa.String(length=128), nullable=True))
    op.add_column("indexing_job_documents", sa.Column("last_dispatch_at", sa.DateTime(timezone=True), nullable=True))

def downgrade() -> None:
    op.drop_column("indexing_job_documents", "last_dispatch_at")
    op.drop_column("indexing_job_documents", "last_dispatch_error")
    op.drop_column("indexing_job_documents", "dispatch_attempts")
