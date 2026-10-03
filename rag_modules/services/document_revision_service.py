from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select

from rag_modules.db.models import (
    DatasetIndexRecord,
    DatasetRecord,
    DocumentRecord,
    DocumentRevisionRecord,
    IndexingJobDocumentRecord,
    IndexingJobRecord,
)
from rag_modules.services.document_service import DocumentNotFoundError, DocumentValidationError


class RevisionConflictError(RuntimeError):
    code = "REVISION_CONFLICT"


@dataclass(frozen=True)
class RevisionJobResult:
    revision: DocumentRevisionRecord
    job: IndexingJobRecord
    job_document_id: str


class DocumentRevisionService:
    def __init__(self, session):
        self.session = session

    async def create_revision_and_job(
        self, dataset_id: str, document_id: str, payload, actor_id: str
    ) -> RevisionJobResult:
        document = await self.session.scalar(
            select(DocumentRecord).where(
                DocumentRecord.dataset_id == dataset_id,
                DocumentRecord.id == document_id,
                DocumentRecord.deleted_at.is_(None),
            ).with_for_update()
        )
        if document is None:
            raise DocumentNotFoundError(document_id)

        latest_version = await self.session.scalar(
            select(func.max(DocumentRevisionRecord.version)).where(
                DocumentRevisionRecord.document_id == document_id
            )
        ) or 0
        if payload.base_version is not None and payload.base_version != latest_version:
            raise RevisionConflictError("Document revision is stale; reload the segments first.")

        items = [item.model_dump(mode="json") for item in payload.segments]
        seen_ids = set()
        for item in items:
            if not item["content"].strip():
                raise DocumentValidationError("Segment content must not be blank.")
            if item["id"] in seen_ids:
                raise DocumentValidationError("Segment IDs must be unique.")
            seen_ids.add(item["id"])
            if item["parent_id"] is not None and item["parent_id"] not in seen_ids:
                raise DocumentValidationError("A child segment must reference an earlier parent segment.")
        if any(item["index_type"] == "child" and item["parent_id"] is None for item in items):
            raise DocumentValidationError("Child segments require a parent segment.")

        active_index = await self.session.scalar(
            select(DatasetIndexRecord).where(
                DatasetIndexRecord.dataset_id == dataset_id,
                DatasetIndexRecord.status == "active",
                DatasetIndexRecord.deleted_at.is_(None),
            ).order_by(DatasetIndexRecord.activated_at.desc()).limit(1).with_for_update()
        )
        if active_index is None:
            raise DocumentValidationError("Document must have an active index before segments can be edited.")

        existing_job = await self.session.scalar(
            select(IndexingJobRecord).where(
                IndexingJobRecord.dataset_id == dataset_id,
                IndexingJobRecord.status.in_(("pending", "queued", "running", "retry_wait")),
                IndexingJobRecord.revision_id.is_not(None),
            ).join(IndexingJobDocumentRecord, IndexingJobDocumentRecord.job_id == IndexingJobRecord.id)
            .where(IndexingJobDocumentRecord.document_id == document_id)
        )
        if existing_job is not None:
            raise RevisionConflictError("This document already has a reindexing task in progress.")

        dataset = await self.session.get(DatasetRecord, dataset_id)
        if dataset is None or dataset.deleted_at is not None:
            raise DocumentNotFoundError(dataset_id)
        revision = DocumentRevisionRecord(
            id=uuid4().hex,
            dataset_id=dataset_id,
            document_id=document_id,
            version=latest_version + 1,
            segments={"items": items},
            created_by=actor_id,
            status="pending",
        )
        job = IndexingJobRecord(
            id=uuid4().hex,
            dataset_id=dataset_id,
            target_index_id=active_index.id,
            revision_id=revision.id,
            job_type="document_reindex",
            scope="selected_documents",
            status="pending",
            indexing_technique=active_index.index_type,
            segmentation_mode=(active_index.process_rule or {}).get("segmentation", {}).get("mode", "general"),
            embedding_model_provider=active_index.embedding_model_provider,
            embedding_model=active_index.embedding_model,
            process_rule=active_index.process_rule or {},
            retrieval_config=active_index.retrieval_config or {},
            total_documents=1,
            created_by=actor_id,
        )
        job_document = IndexingJobDocumentRecord(
            id=uuid4().hex,
            job_id=job.id,
            document_id=document_id,
            status="pending",
        )
        revision.indexing_job_id = job.id
        document.indexing_status = "queued"
        document.updated_by = actor_id
        document.updated_at = __import__("rag_modules.common", fromlist=["utcnow"]).utcnow()
        self.session.add_all([revision, job, job_document])
        await self.session.commit()
        return RevisionJobResult(revision=revision, job=job, job_document_id=job_document.id)
