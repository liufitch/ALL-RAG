"""Indexing job persistence and worker lease operations.

The repository deliberately commits each state transition.  Indexing is a
long-running operation and a worker process can disappear between any two
stages; keeping progress in PostgreSQL makes the task recoverable without
depending on Celery's result backend.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_modules.common import utcnow
from rag_modules.db.models import (
    DatasetIndexRecord,
    DatasetRecord,
    DocumentRecord,
    IndexingJobDocumentRecord,
    IndexingJobRecord,
    ProcessRuleRecord,
    DocumentSegmentRecord,
)


class IndexingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim_job_document(
        self,
        job_document_id: str,
        worker_id: str,
        lease_seconds: int = 300,
    ) -> IndexingJobDocumentRecord | None:
        """Atomically claim a pending row or reclaim an expired lease.

        ``FOR UPDATE`` makes the read/transition a single critical section on
        PostgreSQL. SQLite does not support ``SKIP LOCKED`` but still provides
        a useful serialized write transaction for local development.
        """
        now = utcnow()
        result = await self.session.execute(
            select(IndexingJobDocumentRecord)
            .where(IndexingJobDocumentRecord.id == job_document_id)
            .with_for_update()
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        claimable = row.status in {"pending", "queued", "retry_wait"} and (
            row.available_at is None or row.available_at <= now
        )
        expired = row.status == "running" and (
            row.lease_expires_at is None or row.lease_expires_at <= now
        )
        if not claimable and not expired:
            return None
        row.status = "running"
        row.attempt = int(row.attempt or 0) + 1
        row.worker_id = worker_id
        row.heartbeat_at = now
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        row.started_at = row.started_at or now
        row.updated_at = now
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def heartbeat(
        self,
        job_document_id: str,
        *,
        worker_id: str,
        stage: str | None = None,
        progress: int | None = None,
        processed_segments: int | None = None,
        total_segments: int | None = None,
        embedded_segments: int | None = None,
        lease_seconds: int = 300,
    ) -> None:
        now = utcnow()
        row = await self.session.get(IndexingJobDocumentRecord, job_document_id)
        if row is None or row.status != "running" or row.worker_id != worker_id:
            return
        row.heartbeat_at = now
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        row.updated_at = now
        if stage is not None:
            row.current_stage = stage
        if progress is not None:
            row.progress = max(0, min(100, int(progress)))
        if processed_segments is not None:
            row.processed_segments = max(0, int(processed_segments))
        if total_segments is not None:
            row.total_segments = max(0, int(total_segments))
        if embedded_segments is not None:
            row.embedded_segments = max(0, int(embedded_segments))
        document = await self.session.get(DocumentRecord, row.document_id)
        if document is not None:
            document.indexing_status = {
                "download": "downloading", "parse": "parsing", "split": "splitting",
                "stage": "indexing", "embed-or-keywords": "embedding",
                "vector-upsert": "indexing", "validate": "indexing",
            }.get(stage or "", document.indexing_status)
            document.updated_at = now
        await self.session.commit()

    async def cancellation_requested(self, job_document_id: str) -> bool:
        row = await self.session.get(IndexingJobDocumentRecord, job_document_id)
        if row is None:
            return True
        job = await self.session.get(IndexingJobRecord, row.job_id)
        return bool(job and job.cancel_requested_at is not None) or row.status == "cancelled"

    async def complete_job_document(
        self,
        job_document_id: str,
        *,
        worker_id: str,
        total_segments: int,
        processed_segments: int,
        embedded_segments: int,
        warnings: list[dict[str, Any]] | None = None,
    ) -> None:
        row = await self._owned_running_row(job_document_id, worker_id)
        if row is None:
            return
        now = utcnow()
        row.status = "completed"
        row.progress = 100
        row.current_stage = "validate"
        row.total_segments = total_segments
        row.processed_segments = processed_segments
        row.embedded_segments = embedded_segments
        row.warnings = warnings or None
        row.completed_at = now
        row.heartbeat_at = now
        row.lease_expires_at = None
        row.updated_at = now
        document = await self.session.get(DocumentRecord, row.document_id)
        if document is not None:
            document.indexing_status = "completed"
            document.error = None
            document.updated_at = now
        await self.session.commit()

    async def cancel_job_document(self, job_document_id: str, *, worker_id: str) -> None:
        row = await self._owned_running_row(job_document_id, worker_id)
        if row is None:
            return
        now = utcnow()
        row.status = "cancelled"
        row.completed_at = now
        row.cancelled_at = now
        row.lease_expires_at = None
        row.updated_at = now
        document = await self.session.get(DocumentRecord, row.document_id)
        if document is not None:
            document.indexing_status = "cancelled"
            document.updated_at = now
        await self.session.commit()

    async def fail_job_document(
        self,
        job_document_id: str,
        *,
        worker_id: str,
        error_code: str,
        error: str,
        retryable: bool,
        retry_delay_seconds: int,
    ) -> None:
        row = await self._owned_running_row(job_document_id, worker_id)
        if row is None:
            return
        now = utcnow()
        can_retry = retryable and int(row.attempt or 0) < int(row.max_attempts or 3)
        row.status = "retry_wait" if can_retry else "failed"
        row.available_at = now + timedelta(seconds=retry_delay_seconds) if can_retry else now
        row.error_code = error_code[:128]
        row.error = error[:2000]
        row.lease_expires_at = None
        row.heartbeat_at = now
        row.updated_at = now
        document = await self.session.get(DocumentRecord, row.document_id)
        if document is not None:
            document.indexing_status = "retry_wait" if can_retry else "failed"
            document.error = error[:2000]
            document.updated_at = now
        await self.session.commit()

    async def refresh_job_summary(self, job_id: str) -> None:
        job = await self.session.get(IndexingJobRecord, job_id)
        if job is None:
            return
        result = await self.session.execute(
            select(IndexingJobDocumentRecord).where(
                IndexingJobDocumentRecord.job_id == job_id
            )
        )
        rows = list(result.scalars())
        job.total_documents = len(rows)
        job.processed_documents = sum(row.status in {"completed", "failed", "cancelled"} for row in rows)
        job.completed_documents = sum(row.status == "completed" for row in rows)
        job.failed_documents = sum(row.status == "failed" for row in rows)
        job.progress = int(sum(int(row.progress or 0) for row in rows) / len(rows)) if rows else 0
        active = [row for row in rows if row.status in {"pending", "queued", "running", "retry_wait"}]
        if active:
            job.status = "running" if any(row.status == "running" for row in active) else "queued"
        elif rows and all(row.status == "completed" for row in rows):
            job.status = "completed"
            job.progress = 100
            job.completed_at = job.completed_at or utcnow()
        elif any(row.status == "failed" for row in rows):
            job.status = "partial_success" if job.completed_documents else "failed"
        elif rows and all(row.status == "cancelled" for row in rows):
            job.status = "cancelled"
            job.cancelled_at = job.cancelled_at or utcnow()
        job.current_stage = next((row.current_stage for row in rows if row.status == "running"), job.current_stage)
        job.updated_at = utcnow()
        await self.session.commit()

    async def finalize_indexing_job(self, job_id: str) -> bool:
        """Finalize a terminal job and atomically publish a full-build index.

        The job row is the serialization point.  A build is never made visible
        until every document is terminal and every staged segment passes the
        final consistency checks.  Repeated calls are intentionally idempotent.
        """
        result = await self.session.execute(
            select(IndexingJobRecord)
            .where(IndexingJobRecord.id == job_id)
            .with_for_update()
        )
        job = result.scalar_one_or_none()
        if job is None:
            return False

        documents_result = await self.session.execute(
            select(IndexingJobDocumentRecord)
            .where(IndexingJobDocumentRecord.job_id == job_id)
            .with_for_update()
        )
        job_documents = list(documents_result.scalars())
        terminal_statuses = {"completed", "failed", "cancelled"}
        if not job_documents or any(row.status not in terminal_statuses for row in job_documents):
            return False

        target = None
        if job.target_index_id:
            target = await self.session.get(DatasetIndexRecord, job.target_index_id, with_for_update=True)

        full_build = job.job_type in {"initial_index", "reindex_dataset"}
        if target is None:
            job.status = self._terminal_job_status(job_documents)
            job.completed_at = job.completed_at or utcnow()
            job.updated_at = utcnow()
            await self.session.commit()
            return True

        if not full_build:
            failed = [row for row in job_documents if row.status in {"failed", "cancelled"}]
            if failed:
                job.status = self._terminal_job_status(job_documents)
                job.completed_at = job.completed_at or utcnow()
                job.updated_at = utcnow()
                await self.session.commit()
                return True

            document_ids = [row.document_id for row in job_documents]
            segments_result = await self.session.execute(
                select(DocumentSegmentRecord)
                .where(
                    DocumentSegmentRecord.dataset_id == job.dataset_id,
                    DocumentSegmentRecord.dataset_index_id == target.id,
                    DocumentSegmentRecord.document_id.in_(document_ids),
                    DocumentSegmentRecord.deleted_at.is_(None),
                )
                .with_for_update()
            )
            segments = list(segments_result.scalars())
            staged = [segment for segment in segments if segment.status == "indexing"]
            if not staged or any(
                not self._segment_ready_for_activation(job, segment) for segment in staged
            ):
                job.status = "failed"
                job.error_code = "INDEX_VALIDATION_FAILED"
                job.error = "Built index validation failed."
                job.completed_at = job.completed_at or utcnow()
                job.updated_at = utcnow()
                await self.session.commit()
                return True

            timestamp = utcnow()
            staged_ids = {segment.id for segment in staged}
            for segment in segments:
                if segment.id not in staged_ids and segment.status == "completed":
                    segment.deleted_at = timestamp
                    segment.updated_at = timestamp
            for segment in staged:
                segment.status = "completed"
                segment.updated_at = timestamp
            job.status = "completed"
            job.progress = 100
            job.completed_at = job.completed_at or timestamp
            job.updated_at = timestamp
            await self.session.commit()
            return True

        if target.status == "active" and job.status == "completed":
            return True
        if target.status in {"failed", "retired"} and job.status in terminal_statuses:
            return True

        failed = [row for row in job_documents if row.status in {"failed", "cancelled"}]
        if failed:
            target.status = "failed"
            target.updated_at = utcnow()
            job.status = "cancelled" if all(row.status == "cancelled" for row in job_documents) else "failed"
            job.completed_at = job.completed_at or utcnow()
            job.updated_at = utcnow()
            await self.session.commit()
            return True

        document_ids = [row.document_id for row in job_documents]
        segments_result = await self.session.execute(
            select(DocumentSegmentRecord)
            .where(
                DocumentSegmentRecord.dataset_id == job.dataset_id,
                DocumentSegmentRecord.dataset_index_id == target.id,
                DocumentSegmentRecord.document_id.in_(document_ids),
                DocumentSegmentRecord.deleted_at.is_(None),
            )
            .with_for_update()
        )
        segments = list(segments_result.scalars())
        by_document: dict[str, list[DocumentSegmentRecord]] = {}
        for segment in segments:
            by_document.setdefault(segment.document_id, []).append(segment)
        if any(not by_document.get(document_id) for document_id in document_ids) or any(
            not self._segment_ready_for_activation(job, segment) for segment in segments
        ):
            target.status = "failed"
            target.updated_at = utcnow()
            job.status = "failed"
            job.error_code = "INDEX_VALIDATION_FAILED"
            job.error = "Built index validation failed."
            job.completed_at = job.completed_at or utcnow()
            job.updated_at = utcnow()
            await self.session.commit()
            return True

        timestamp = utcnow()
        active_result = await self.session.execute(
            select(DatasetIndexRecord)
            .where(
                DatasetIndexRecord.dataset_id == job.dataset_id,
                DatasetIndexRecord.status == "active",
                DatasetIndexRecord.deleted_at.is_(None),
                DatasetIndexRecord.id != target.id,
            )
            .with_for_update()
        )
        previous_indexes = list(active_result.scalars())
        previous_ids = [index.id for index in previous_indexes]
        if previous_ids:
            previous_segments_result = await self.session.execute(
                select(DocumentSegmentRecord).where(
                    DocumentSegmentRecord.dataset_id == job.dataset_id,
                    DocumentSegmentRecord.dataset_index_id.in_(previous_ids),
                    DocumentSegmentRecord.deleted_at.is_(None),
                ).with_for_update()
            )
            for segment in previous_segments_result.scalars():
                segment.deleted_at = timestamp
                segment.updated_at = timestamp
            for previous in previous_indexes:
                previous.status = "retired"
                previous.retired_at = previous.retired_at or timestamp
                previous.updated_at = timestamp

        for segment in segments:
            segment.status = "completed"
            segment.updated_at = timestamp
        target.status = "active"
        target.activated_at = target.activated_at or timestamp
        target.updated_at = timestamp
        job.status = "completed"
        job.progress = 100
        job.completed_at = job.completed_at or timestamp
        job.updated_at = timestamp
        await self.session.commit()
        return True

    @staticmethod
    def _terminal_job_status(rows: list[IndexingJobDocumentRecord]) -> str:
        if all(row.status == "completed" for row in rows):
            return "completed"
        if all(row.status == "cancelled" for row in rows):
            return "cancelled"
        return "partial_success" if any(row.status == "completed" for row in rows) else "failed"

    @staticmethod
    def _segment_ready_for_activation(job: IndexingJobRecord, segment: DocumentSegmentRecord) -> bool:
        if segment.status != "indexing":
            return False
        if job.indexing_technique == "economy":
            return segment.embedding_status == "not_required"
        if segment.index_type == "parent":
            return segment.embedding_status == "not_required"
        return segment.embedding_status == "completed"

    async def recover_expired_documents(
        self, *, worker_id: str = "recovery", lease_seconds: int = 300, limit: int = 100
    ) -> list[str]:
        now = utcnow()
        result = await self.session.execute(
            select(IndexingJobDocumentRecord)
            .where(
                IndexingJobDocumentRecord.status == "running",
                IndexingJobDocumentRecord.lease_expires_at <= now,
            )
            .order_by(IndexingJobDocumentRecord.lease_expires_at.asc())
            .limit(limit)
            .with_for_update()
        )
        recovered: list[str] = []
        for row in result.scalars():
            row.status = "retry_wait" if int(row.attempt or 0) < int(row.max_attempts or 3) else "failed"
            row.available_at = now if row.status == "retry_wait" else now
            row.worker_id = worker_id
            row.lease_expires_at = None
            row.heartbeat_at = now
            row.updated_at = now
            if row.status == "failed":
                row.error_code = "WORKER_LEASE_EXPIRED"
                row.error = "The indexing worker lease expired." 
            recovered.append(row.id)
        await self.session.commit()
        return recovered

    async def execution_context(self, job_document_id: str):
        """Load the immutable job snapshot and source references for a worker."""
        from sqlalchemy.orm import aliased

        job_document = await self.session.get(IndexingJobDocumentRecord, job_document_id)
        if job_document is None:
            return None
        job = await self.session.get(IndexingJobRecord, job_document.job_id)
        if job is None:
            return None
        document = await self.session.get(DocumentRecord, job_document.document_id)
        if document is None or document.deleted_at is not None:
            return None
        index = await self.session.get(DatasetIndexRecord, job.target_index_id) if job.target_index_id else None
        dataset = await self.session.get(DatasetRecord, job.dataset_id)
        return job_document, job, document, index, dataset

    async def ensure_compatibility_job_document(self, dataset_id: str, document_id: str) -> str | None:
        """Create a default durable job for legacy upload messages.

        Older upload clients publish ``dataset_id``/``document_id`` directly.
        This bridge lets those messages enter the same durable pipeline while
        newer callers can create fully configured jobs explicitly.
        """
        existing = await self.session.execute(
            select(IndexingJobDocumentRecord)
            .join(IndexingJobRecord, IndexingJobRecord.id == IndexingJobDocumentRecord.job_id)
            .where(
                IndexingJobDocumentRecord.document_id == document_id,
                IndexingJobRecord.dataset_id == dataset_id,
                IndexingJobDocumentRecord.status.in_(("pending", "queued", "running", "retry_wait")),
            )
            .order_by(IndexingJobDocumentRecord.created_at.desc())
        )
        found = existing.scalars().first()
        if found is not None:
            return found.id
        document = await self.session.get(DocumentRecord, document_id)
        dataset = await self.session.get(DatasetRecord, dataset_id)
        if document is None or dataset is None or document.deleted_at is not None or dataset.deleted_at is not None:
            return None

        import hashlib
        import json
        from uuid import uuid4

        technique = dataset.indexing_technique if dataset.indexing_technique in {"high_quality", "economy"} else "high_quality"
        embedding_model = dataset.embedding_model or "bge-m3" if technique == "high_quality" else None
        process_rule = {
            "segmentation": {
                "mode": "general",
                "max_chunk_length": 1024,
                "overlap": 100,
                "separator": "\n",
            }
        }
        retrieval_config: dict[str, Any] = {}
        config_hash = hashlib.sha256(
            json.dumps({"technique": technique, "model": embedding_model, "process_rule": process_rule, "retrieval": retrieval_config}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        job_id = uuid4().hex
        target_id = uuid4().hex
        rule_id = uuid4().hex
        rule = ProcessRuleRecord(
            id=rule_id,
            dataset_id=dataset_id,
            version=1,
            mode="general",
            rules=process_rule["segmentation"],
            config_hash=config_hash,
            created_by=document.created_by or "system",
        )
        self.session.add(rule)
        document.dataset_process_rule_id = rule_id
        job = IndexingJobRecord(
            id=job_id,
            dataset_id=dataset_id,
            target_index_id=target_id,
            job_type="initial_index",
            scope="selected_documents",
            status="pending",
            indexing_technique=technique,
            segmentation_mode="general",
            embedding_model_provider="openai_compatible" if embedding_model else None,
            embedding_model=embedding_model,
            process_rule=process_rule,
            retrieval_config=retrieval_config,
            total_documents=1,
            created_by=document.created_by or "system",
        )
        self.session.add(job)
        index = DatasetIndexRecord(
            id=target_id,
            dataset_id=dataset_id,
            created_by_job_id=job_id,
            index_type=technique,
            status="building",
            embedding_model_provider="openai_compatible" if embedding_model else None,
            embedding_model=embedding_model,
            vector_store_provider="milvus" if technique == "high_quality" else None,
            collection_name=f"graph_rag_{dataset_id}_{target_id}" if technique == "high_quality" else None,
            process_rule=process_rule,
            retrieval_config=retrieval_config,
            config_hash=config_hash,
        )
        self.session.add(index)
        job_document = IndexingJobDocumentRecord(
            id=uuid4().hex,
            job_id=job_id,
            document_id=document_id,
            status="pending",
            max_attempts=3,
        )
        self.session.add(job_document)
        await self.session.commit()
        return job_document.id

    async def _owned_running_row(self, job_document_id: str, worker_id: str):
        result = await self.session.execute(
            select(IndexingJobDocumentRecord)
            .where(
                IndexingJobDocumentRecord.id == job_document_id,
                IndexingJobDocumentRecord.status == "running",
                IndexingJobDocumentRecord.worker_id == worker_id,
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()
