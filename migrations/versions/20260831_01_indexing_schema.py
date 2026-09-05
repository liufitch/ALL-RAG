"""添加知识库索引的持久化数据结构。

版本标识：20260831_01
前置版本：无
创建日期：2026-08-31
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260831_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUSINESS_TABLES = {"datasets", "documents", "document_segments"}


def _check_existing_schema() -> None:
    if op.get_context().as_sql:
        return

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    existing_tables = set(sa.inspect(bind).get_table_names())
    missing_tables = sorted(_BUSINESS_TABLES - existing_tables)
    if missing_tables:
        raise RuntimeError(
            "indexing schema migration requires existing business tables: "
            + ", ".join(missing_tables)
        )

    vector_extension_exists = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
    ).scalar_one()
    if not vector_extension_exists:
        raise RuntimeError("indexing schema migration requires the PostgreSQL vector extension")


def upgrade() -> None:
    _check_existing_schema()

    # 稍后再添加目标索引外键，以打破任务与索引之间的循环依赖。
    op.create_table(
        "indexing_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("target_index_id", sa.String(length=36), nullable=True),
        sa.Column("retry_of_job_id", sa.String(length=36), nullable=True),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("current_stage", sa.String(length=64), nullable=True),
        sa.Column("indexing_technique", sa.String(length=32), nullable=False),
        sa.Column("segmentation_mode", sa.String(length=32), nullable=False),
        sa.Column("embedding_model_provider", sa.String(length=255), nullable=True),
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
        sa.Column("process_rule", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("retrieval_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("total_documents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("processed_documents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completed_documents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_documents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_segments", sa.Integer(), server_default="0", nullable=False),
        sa.Column("processed_segments", sa.Integer(), server_default="0", nullable=False),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("celery_task_id", sa.String(length=255), nullable=True),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "job_type IN ('initial_index', 'add_documents', 'reindex_documents', 'reindex_dataset')",
            name="ck_indexing_jobs_job_type",
        ),
        sa.CheckConstraint(
            "scope IN ('selected_documents', 'entire_dataset')",
            name="ck_indexing_jobs_scope",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'queued', 'running', 'completed', 'partial_success', "
            "'failed', 'retry_wait', 'cancelled')",
            name="ck_indexing_jobs_status",
        ),
        sa.CheckConstraint(
            "indexing_technique IN ('high_quality', 'economy')",
            name="ck_indexing_jobs_technique",
        ),
        sa.CheckConstraint(
            "segmentation_mode IN ('general', 'parent_child')",
            name="ck_indexing_jobs_segmentation_mode",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["datasets.id"], name="fk_indexing_jobs_dataset"
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_job_id"], ["indexing_jobs.id"], name="fk_indexing_jobs_retry_of"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_indexing_jobs"),
    )
    op.create_index(
        "ix_indexing_jobs_status_available",
        "indexing_jobs",
        ["status", "available_at"],
    )
    op.create_index(
        "ix_indexing_jobs_running_lease",
        "indexing_jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "dataset_indexes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("created_by_job_id", sa.String(length=36), nullable=False),
        sa.Column("index_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="building", nullable=False),
        sa.Column("embedding_model_provider", sa.String(length=255), nullable=True),
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
        sa.Column("embedding_dimension", sa.Integer(), nullable=True),
        sa.Column("distance_metric", sa.String(length=32), nullable=True),
        sa.Column("vector_store_provider", sa.String(length=64), nullable=True),
        sa.Column("collection_name", sa.String(length=255), nullable=True),
        sa.Column("process_rule", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("retrieval_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "index_type IN ('high_quality', 'economy')",
            name="ck_dataset_indexes_index_type",
        ),
        sa.CheckConstraint(
            "status IN ('building', 'active', 'retired', 'failed', 'deleting')",
            name="ck_dataset_indexes_status",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_job_id"],
            ["indexing_jobs.id"],
            name="fk_dataset_indexes_created_by_job",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["datasets.id"], name="fk_dataset_indexes_dataset"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dataset_indexes"),
    )

    op.create_foreign_key(
        "fk_indexing_jobs_target_index",
        "indexing_jobs",
        "dataset_indexes",
        ["target_index_id"],
        ["id"],
    )

    op.create_table(
        "indexing_job_documents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("current_stage", sa.String(length=64), nullable=True),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_segments", sa.Integer(), server_default="0", nullable=False),
        sa.Column("processed_segments", sa.Integer(), server_default="0", nullable=False),
        sa.Column("embedded_segments", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
        sa.Column("celery_task_id", sa.String(length=255), nullable=True),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'queued', 'running', 'completed', 'failed', 'retry_wait', 'cancelled')",
            name="ck_indexing_job_documents_status",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_indexing_job_documents_document",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["indexing_jobs.id"],
            name="fk_indexing_job_documents_job",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_indexing_job_documents"),
        sa.UniqueConstraint("job_id", "document_id", name="uq_indexing_job_document"),
    )
    op.create_index(
        "ix_indexing_job_documents_status_available",
        "indexing_job_documents",
        ["status", "available_at"],
    )
    op.create_index(
        "ix_indexing_job_documents_running_lease",
        "indexing_job_documents",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
    )

    op.add_column(
        "document_segments", sa.Column("dataset_index_id", sa.String(length=36), nullable=True)
    )
    op.add_column(
        "document_segments", sa.Column("indexing_job_id", sa.String(length=36), nullable=True)
    )
    op.add_column(
        "document_segments",
        sa.Column("source_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "document_segments", sa.Column("content_hash", sa.String(length=64), nullable=True)
    )
    op.create_foreign_key(
        "fk_document_segments_dataset_index",
        "document_segments",
        "dataset_indexes",
        ["dataset_index_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_document_segments_indexing_job",
        "document_segments",
        "indexing_jobs",
        ["indexing_job_id"],
        ["id"],
    )

    op.create_index(
        "uq_dataset_indexes_one_active",
        "dataset_indexes",
        ["dataset_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_document_segments_active_index_document",
        "document_segments",
        ["dataset_index_id", "document_id"],
        postgresql_where=sa.text("status = 'completed' AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_document_segments_keywords_gin",
        "document_segments",
        ["keywords"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_document_segments_keywords_gin", table_name="document_segments")
    op.drop_index("ix_document_segments_active_index_document", table_name="document_segments")
    op.drop_index("uq_dataset_indexes_one_active", table_name="dataset_indexes")

    op.drop_constraint(
        "fk_document_segments_indexing_job", "document_segments", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_document_segments_dataset_index", "document_segments", type_="foreignkey"
    )
    op.drop_column("document_segments", "content_hash")
    op.drop_column("document_segments", "source_metadata")
    op.drop_column("document_segments", "indexing_job_id")
    op.drop_column("document_segments", "dataset_index_id")

    op.drop_index(
        "ix_indexing_job_documents_running_lease", table_name="indexing_job_documents"
    )
    op.drop_index(
        "ix_indexing_job_documents_status_available", table_name="indexing_job_documents"
    )
    op.drop_table("indexing_job_documents")

    op.drop_constraint(
        "fk_indexing_jobs_target_index", "indexing_jobs", type_="foreignkey"
    )
    op.drop_table("dataset_indexes")
    op.drop_index("ix_indexing_jobs_running_lease", table_name="indexing_jobs")
    op.drop_index("ix_indexing_jobs_status_available", table_name="indexing_jobs")
    op.drop_table("indexing_jobs")
