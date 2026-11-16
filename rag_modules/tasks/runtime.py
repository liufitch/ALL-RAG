"""Task-scoped async runtime for Celery workers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from rag_modules.config.settings import settings

logger = logging.getLogger(__name__)


class DocumentCleanupCompensationPending(RuntimeError):
    """The writer finished but its durable cleanup invalidation needs retry."""


def run_indexing_task(job_document_id: str, *, dataset_id: str | None = None, document_id: str | None = None) -> None:
    asyncio.run(_run_indexing_task(job_document_id, dataset_id=dataset_id, document_id=document_id))


def run_finalize_indexing_job(job_id: str) -> None:
    asyncio.run(_run_finalize_indexing_job(job_id))


async def _run_indexing_task(job_document_id: str, *, dataset_id: str | None = None, document_id: str | None = None) -> None:
    engine = create_async_engine(
        settings.sqlalchemy_database_uri,
        poolclass=NullPool, hide_parameters=True,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    context_id = job_document_id
    try:
        async with session_factory() as session:
            from .indexing_tasks import build_task_runner

            runner = await build_task_runner(
                session,
                job_document_id=job_document_id,
                dataset_id=dataset_id,
                document_id=document_id,
            )
            if runner is None:
                logger.error("indexing_runner_not_built job_document_id=%s dataset_id=%s document_id=%s", job_document_id, dataset_id, document_id)
            else:
                context_id = job_document_id
                if dataset_id and document_id:
                    from rag_modules.repositories.indexing_repository import IndexingRepository
                    resolved = await IndexingRepository(session).ensure_compatibility_job_document(dataset_id, document_id)
                    context_id = resolved or job_document_id
                await runner.run(context_id)
    finally:
        try:
            # Lease recovery cannot stop an upsert already running remotely.
            # After the runner has drained its external work, invalidate any
            # cleanup that may have completed in the meantime. A fresh session
            # also covers early exits/redelivery for an already-deleted source.
            from sqlalchemy import select, update
            from rag_modules.db.models import DocumentRecord, IndexingJobDocumentRecord

            async with session_factory() as cleanup_session:
                filters = [DocumentRecord.deleted_at.is_not(None)]
                if dataset_id and document_id:
                    filters.extend((DocumentRecord.dataset_id == dataset_id, DocumentRecord.id == document_id))
                else:
                    filters.append(DocumentRecord.id.in_(select(IndexingJobDocumentRecord.document_id).where(
                        IndexingJobDocumentRecord.id == context_id,
                    )))
                await cleanup_session.execute(update(DocumentRecord).where(*filters).values(
                    cleanup_completed_at=None, cleanup_last_attempt_at=None,
                ))
                await cleanup_session.commit()
        except Exception:
            raise DocumentCleanupCompensationPending("Document cleanup compensation must be retried.") from None
        finally:
            await engine.dispose()


async def _run_finalize_indexing_job(job_id: str) -> None:
    engine = create_async_engine(
        settings.sqlalchemy_database_uri,
        poolclass=NullPool, hide_parameters=True,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with session_factory() as session:
            from rag_modules.repositories.indexing_repository import IndexingRepository

            await IndexingRepository(session).finalize_indexing_job(job_id)
    finally:
        await engine.dispose()
