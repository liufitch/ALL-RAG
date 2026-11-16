"""Pin the revision input independently for each document in an indexing job."""
from alembic import op
import sqlalchemy as sa

revision = "20261116_02"
down_revision = "20261116_01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("indexing_job_documents", sa.Column("revision_id", sa.String(36), nullable=True))
    op.create_foreign_key("fk_indexing_job_documents_revision", "indexing_job_documents", "document_revisions", ["revision_id"], ["id"])
    # Preserve the explicit binding of legacy single-document revision jobs.
    op.execute("""
        UPDATE indexing_job_documents AS d SET revision_id = j.revision_id
        FROM indexing_jobs AS j, document_revisions AS r
        WHERE d.job_id = j.id AND j.revision_id = r.id
          AND r.document_id = d.document_id AND r.dataset_id = j.dataset_id
    """)


def downgrade():
    op.drop_constraint("fk_indexing_job_documents_revision", "indexing_job_documents", type_="foreignkey")
    op.drop_column("indexing_job_documents", "revision_id")
