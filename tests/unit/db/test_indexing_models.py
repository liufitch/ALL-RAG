from sqlalchemy import ARRAY, JSON, Text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex

from rag_modules.db.models import (
    DatasetIndexRecord,
    DocumentSegmentRecord,
    IndexingJobDocumentRecord,
    IndexingJobRecord,
)


def test_indexing_models_map_to_approved_tables():
    assert DatasetIndexRecord.__tablename__ == "dataset_indexes"
    assert IndexingJobRecord.__tablename__ == "indexing_jobs"
    assert IndexingJobDocumentRecord.__tablename__ == "indexing_job_documents"
    assert "dataset_index_id" in DocumentSegmentRecord.__table__.c
    assert "indexing_job_id" in DocumentSegmentRecord.__table__.c
    assert "source_metadata" in DocumentSegmentRecord.__table__.c
    assert "content_hash" in DocumentSegmentRecord.__table__.c


def test_job_document_identity_is_unique():
    names = {constraint.name for constraint in IndexingJobDocumentRecord.__table__.constraints}
    assert "uq_indexing_job_document" in names


def test_job_documents_have_independent_worker_dispatch_and_lease_state():
    column_names = set(IndexingJobDocumentRecord.__table__.c.keys())

    assert {
        "available_at",
        "heartbeat_at",
        "lease_expires_at",
        "worker_id",
        "celery_task_id",
        "warnings",
    } <= column_names


def test_new_json_columns_use_jsonb_on_postgresql_with_generic_fallback():
    json_columns = (
        DatasetIndexRecord.__table__.c.process_rule,
        DatasetIndexRecord.__table__.c.retrieval_config,
        IndexingJobRecord.__table__.c.process_rule,
        IndexingJobRecord.__table__.c.retrieval_config,
        IndexingJobDocumentRecord.__table__.c.warnings,
        DocumentSegmentRecord.__table__.c.source_metadata,
    )

    for column in json_columns:
        assert isinstance(column.type, JSON)
        assert isinstance(column.type.dialect_impl(postgresql.dialect()), postgresql.JSONB)


def test_segment_keywords_keep_postgresql_text_array_and_gin_with_sqlite_json_variant():
    keywords = DocumentSegmentRecord.__table__.c.keywords
    postgresql_type = keywords.type.dialect_impl(postgresql.dialect())
    sqlite_type = keywords.type.dialect_impl(sqlite.dialect())
    keyword_index = next(
        index
        for index in DocumentSegmentRecord.__table__.indexes
        if index.name == "ix_document_segments_keywords_gin"
    )
    postgresql_ddl = str(CreateIndex(keyword_index).compile(dialect=postgresql.dialect()))

    assert isinstance(postgresql_type, ARRAY)
    assert isinstance(postgresql_type.item_type, Text)
    assert isinstance(sqlite_type, JSON)
    assert postgresql_ddl == (
        "CREATE INDEX ix_document_segments_keywords_gin "
        "ON document_segments USING gin (keywords)"
    )
