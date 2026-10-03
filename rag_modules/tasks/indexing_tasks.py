"""索引 Worker 的 Celery 入口和可测试的持久化任务运行器。"""

from __future__ import annotations

import asyncio
import inspect
import os
from typing import Any, Callable
from uuid import uuid4

from .celery_app import celery_app
from rag_modules.indexing.engine import DocumentIndexingError
from rag_modules.indexing.progress import DatabaseProgressReporter, IndexingCancelled
from rag_modules.indexing.models import IndexDocumentCommand, SegmentStagingCommand
from rag_modules.segmentation.models import PreviewSegment
from rag_modules.indexing.keywords import KeywordExtractor
from rag_modules.indexing.target_coordinator import IndexTargetCoordinator
from rag_modules.object_storage.factory import get_object_storage
from rag_modules.parsing.factory import get_parser_registry
from rag_modules.repositories.segment_repository import SegmentRepository
from rag_modules.segmentation import GeneralSegmentationConfig, ParentChildSegmentationConfig, Segmenter
from rag_modules.embeddings import OpenAICompatibleEmbeddingClient
from rag_modules.vector_stores.factory import get_vector_store
from rag_modules.config.settings import settings
from rag_modules.common import utcnow


_RETRY_DELAYS = (30, 120, 600)


class IndexingTaskRunner:
    """Run one claimed job-document and persist every terminal transition.

    Dependencies are injected so the state machine can be exercised without a
    broker or external services.  The Celery adapter below supplies the real
    repository and engine in a worker process.
    """

    def __init__(self, *, repository, engine, command_factory: Callable[[Any], Any], worker_id: str | None = None):
        self.repository = repository
        self.engine = engine
        self.command_factory = command_factory
        self.worker_id = worker_id or f"worker-{os.getpid()}-{uuid4().hex[:8]}"

    async def run(self, job_document_id: str) -> None:
        row = await self.repository.claim_job_document(
            job_document_id, self.worker_id, lease_seconds=300
        )
        if row is None:
            return
        progress = DatabaseProgressReporter(
            self.repository,
            job_document_id=job_document_id,
            worker_id=self.worker_id,
            lease_seconds=300,
        )
        try:
            command = self.command_factory(row)
            result = self.engine.run(command, progress)
            if inspect.isawaitable(result):
                result = await result
            await self.repository.complete_job_document(
                job_document_id,
                worker_id=self.worker_id,
                total_segments=int(result.total_segments),
                processed_segments=int(result.total_indexable_segments),
                embedded_segments=int(result.vector_count),
                warnings=[{"stage": stage, "code": code} for stage, code in result.warnings],
            )
        except IndexingCancelled:
            await self.repository.cancel_job_document(job_document_id, worker_id=self.worker_id)
        except DocumentIndexingError as exc:
            delay = _RETRY_DELAYS[min(max(int(row.attempt or 1) - 1, 0), len(_RETRY_DELAYS) - 1)]
            await self.repository.fail_job_document(
                job_document_id,
                worker_id=self.worker_id,
                error_code=exc.code,
                error=exc.safe_message,
                retryable=exc.retryable,
                retry_delay_seconds=delay,
            )
        except Exception:
            # Unknown infrastructure failures are retryable, but the exception
            # text is never persisted because it may contain credentials or
            # source content from a third-party library.
            delay = _RETRY_DELAYS[min(max(int(row.attempt or 1) - 1, 0), len(_RETRY_DELAYS) - 1)]
            await self.repository.fail_job_document(
                job_document_id,
                worker_id=self.worker_id,
                error_code="INDEXING_WORKER_ERROR",
                error="Indexing worker failed temporarily.",
                retryable=True,
                retry_delay_seconds=delay,
            )
        finally:
            await self.repository.refresh_job_summary(row.job_id)
            finalize = getattr(self.repository, "finalize_indexing_job", None)
            if finalize is not None:
                await finalize(row.job_id)


@celery_app.task(name="rag_modules.tasks.indexing_tasks.index_document")
def index_document(*, job_document_id: str | None = None, dataset_id: str | None = None, document_id: str | None = None) -> None:
    """Process a durable job-document identifier.

    ``dataset_id``/``document_id`` are retained for compatibility with the
    original upload publisher. New publishers should send ``job_document_id``;
    the compatibility resolver creates or locates that durable row before
    entering the same claim state machine.
    """
    identifier = job_document_id or document_id
    if not identifier:
        return
    from .runtime import run_indexing_task

    run_indexing_task(identifier, dataset_id=dataset_id, document_id=document_id)


@celery_app.task(name="rag_modules.tasks.indexing_tasks.finalize_indexing_job")
def finalize_indexing_job(*, job_id: str | None = None) -> None:
    """Retry the database-only finalization step for a durable indexing job."""
    if not job_id:
        return
    from .runtime import run_finalize_indexing_job

    run_finalize_indexing_job(job_id)


async def build_task_runner(session, *, job_document_id: str, dataset_id: str | None = None, document_id: str | None = None):
    from rag_modules.repositories.indexing_repository import IndexingRepository

    repository = IndexingRepository(session)
    if dataset_id and document_id:
        resolved = await repository.ensure_compatibility_job_document(dataset_id, document_id)
        if resolved:
            job_document_id = resolved
    context = await repository.execution_context(job_document_id)
    if context is None:
        return None
    job_document, job, document, index, _dataset = context
    if index is None:
        return None
    revision_segments = None
    if job.revision_id:
        from rag_modules.db.models import DocumentRevisionRecord

        revision = await session.get(DocumentRevisionRecord, job.revision_id)
        snapshot = revision.segments.get("items", []) if revision and isinstance(revision.segments, dict) else []
        revision_segments = tuple(
            PreviewSegment(
                local_id=item["id"],
                parent_local_id=item.get("parent_id"),
                position=int(item["position"]),
                content=item["content"],
                source_metadata={
                    **dict(item.get("source_metadata") or {}),
                    "revision_id": revision.id if revision else job.revision_id,
                },
                index_type=item.get("index_type") or "general",
                question=item.get("question"),
                answer=item.get("answer"),
                keywords=tuple(item.get("keywords") or ()),
            )
            for item in snapshot
        )

    process = (job.process_rule or {}).get("segmentation", job.process_rule or {})
    if job.segmentation_mode == "parent_child":
        segmentation = ParentChildSegmentationConfig(
            parent_mode=process.get("parent_mode", "paragraph"),
            parent_max_length=int(process.get("parent_max_chunk_length", process.get("parent_max_length", settings.indexing.parent_max_chunk_length))),
            child_max_length=int(process.get("child_max_chunk_length", process.get("child_max_length", settings.indexing.child_max_chunk_length))),
            child_overlap=int(process.get("child_overlap", settings.indexing.child_overlap)),
            separator=process.get("separator", "\n"),
        )
    else:
        segmentation = GeneralSegmentationConfig(
            max_chunk_length=int(process.get("max_chunk_length", settings.indexing.general_max_chunk_length)),
            overlap=int(process.get("overlap", settings.indexing.general_overlap)),
            separator=process.get("separator", "\n"),
        )
    info = document.data_source_info or {}
    extension = __import__("pathlib").PurePosixPath(document.name).suffix.lower()
    embedding = OpenAICompatibleEmbeddingClient(settings.embedding) if job.indexing_technique == "high_quality" else _NoEmbedding()
    vector_store = get_vector_store() if job.indexing_technique == "high_quality" else _NoVectorStore()
    engine = __import__("rag_modules.indexing.engine", fromlist=["DocumentIndexingEngine"]).DocumentIndexingEngine(
        object_storage=get_object_storage(),
        parser_registry=get_parser_registry(),
        segmenter=Segmenter(),
        segment_repository=SegmentRepository(session),
        embedding=embedding,
        vector_target_resolver=IndexTargetCoordinator(session, vector_store),
        vector_store=vector_store,
        keyword_extractor=KeywordExtractor(),
    )

    def command_factory(_row):
        return IndexDocumentCommand(
            staging=SegmentStagingCommand(
                dataset_id=job.dataset_id,
                dataset_index_id=index.id,
                document_id=document.id,
                indexing_job_id=job.id,
                indexing_technique=job.indexing_technique,
                segmentation_mode=job.segmentation_mode,
            ),
            object_key=info["object_key"],
            filename=document.name,
            extension=extension,
            segmentation_config=segmentation,
            embedding_model=job.embedding_model,
            embedding_batch_size=settings.embedding.get_model(job.embedding_model).batch_size if job.embedding_model else 1,
            vector_batch_size=settings.vector_store.batch_size,
            collection_name=index.collection_name if job.indexing_technique == "high_quality" and index.embedding_dimension else None,
            expected_dimension=index.embedding_dimension,
            revision_segments=revision_segments,
            segment_namespace=job.id if job.job_type == "document_reindex" else None,
        )

    return IndexingTaskRunner(repository=repository, engine=engine, command_factory=command_factory)


class _NoEmbedding:
    async def embed(self, model_id, texts):
        raise DocumentIndexingError("EMBEDDING_NOT_CONFIGURED", False, "Embedding is not configured for this index.")


class _NoVectorStore:
    def ensure_collection(self, *args, **kwargs):
        raise DocumentIndexingError("VECTOR_STORE_NOT_CONFIGURED", False, "Vector store is not configured for this index.")
