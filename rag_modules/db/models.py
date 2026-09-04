from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from rag_modules.common import utcnow
from rag_modules.db.base import Base


def _json_type() -> JSON:
    return JSON().with_variant(JSONB(), "postgresql")


class DatasetRecord(Base):
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    permission: Mapped[str] = mapped_column(String(255), nullable=False)
    dataset_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    indexing_technique: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_model_provider: Mapped[str | None] = mapped_column(String(255), nullable=True)
    retrieval_model_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    partial_user_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DocumentRecord(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    data_source_type: Mapped[str] = mapped_column(String(255), nullable=False)
    data_source_info: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    dataset_process_rule_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_from: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    tokens: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    indexing_status: Mapped[str] = mapped_column(String(255), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    archived_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    archived_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentSegmentRecord(Base):
    __tablename__ = "document_segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(36), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(255), nullable=False)
    index_type: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_status: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # PostgreSQL retains its GIN-indexable ARRAY; SQLite integration tests use
    # JSON because its dialect cannot compile PostgreSQL ARRAY DDL.
    keywords: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text).with_variant(JSON, "sqlite"), nullable=True
    )
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    dataset_index_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("dataset_indexes.id", name="fk_document_segments_dataset_index"),
        nullable=True,
    )
    indexing_job_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("indexing_jobs.id", name="fk_document_segments_indexing_job"),
        nullable=True,
    )
    source_metadata: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index(
            "ix_document_segments_active_index_document",
            "dataset_index_id",
            "document_id",
            postgresql_where=(deleted_at.is_(None) & (status == "completed")),
        ),
        Index("ix_document_segments_keywords_gin", "keywords", postgresql_using="gin"),
    )


class IndexingJobRecord(Base):
    __tablename__ = "indexing_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("datasets.id", name="fk_indexing_jobs_dataset"),
        nullable=False,
    )
    target_index_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("dataset_indexes.id", name="fk_indexing_jobs_target_index", use_alter=True),
        nullable=True,
    )
    retry_of_job_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("indexing_jobs.id", name="fk_indexing_jobs_retry_of"),
        nullable=True,
    )
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    indexing_technique: Mapped[str] = mapped_column(String(32), nullable=False)
    segmentation_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    embedding_model_provider: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    process_rule: Mapped[dict] = mapped_column(_json_type(), nullable=False)
    retrieval_config: Mapped[dict] = mapped_column(_json_type(), nullable=False)
    total_documents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_documents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_documents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_documents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_segments: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_segments: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_indexing_jobs_status_available", "status", "available_at"),
        Index(
            "ix_indexing_jobs_running_lease",
            "lease_expires_at",
            postgresql_where=(status == "running"),
        ),
    )


class DatasetIndexRecord(Base):
    __tablename__ = "dataset_indexes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("datasets.id", name="fk_dataset_indexes_dataset"),
        nullable=False,
    )
    created_by_job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("indexing_jobs.id", name="fk_dataset_indexes_created_by_job"),
        nullable=False,
    )
    index_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="building", nullable=False)
    embedding_model_provider: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distance_metric: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vector_store_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    collection_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    process_rule: Mapped[dict] = mapped_column(_json_type(), nullable=False)
    retrieval_config: Mapped[dict] = mapped_column(_json_type(), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "uq_dataset_indexes_one_active",
            "dataset_id",
            unique=True,
            postgresql_where=((status == "active") & deleted_at.is_(None)),
        ),
    )


class IndexingJobDocumentRecord(Base):
    __tablename__ = "indexing_job_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("indexing_jobs.id", name="fk_indexing_job_documents_job", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", name="fk_indexing_job_documents_document"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_segments: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_segments: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    embedded_segments: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    warnings: Mapped[list[dict] | None] = mapped_column(_json_type(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("job_id", "document_id", name="uq_indexing_job_document"),
        Index("ix_indexing_job_documents_status_available", "status", "available_at"),
        Index(
            "ix_indexing_job_documents_running_lease",
            "lease_expires_at",
            postgresql_where=(status == "running"),
        ),
    )
