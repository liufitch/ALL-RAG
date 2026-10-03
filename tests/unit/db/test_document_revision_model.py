from __future__ import annotations

from sqlalchemy import inspect

from rag_modules.db.models import DocumentRevisionRecord


def test_document_revision_has_immutable_snapshot_schema():
    columns = {column.name for column in DocumentRevisionRecord.__table__.columns}

    assert columns == {
        "id",
        "dataset_id",
        "document_id",
        "version",
        "segments",
        "created_by",
        "created_at",
        "indexing_job_id",
        "status",
        "error",
    }
    assert any(
        constraint.name == "uq_document_revisions_document_version"
        for constraint in DocumentRevisionRecord.__table__.constraints
    )


def test_document_revision_snapshot_is_json():
    assert DocumentRevisionRecord.__table__.c.segments.type.python_type is dict
