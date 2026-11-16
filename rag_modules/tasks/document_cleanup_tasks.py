"""Idempotent asset cleanup with durable recovery from PostgreSQL tombstones."""

from __future__ import annotations

import asyncio
import logging

from .celery_app import celery_app
from rag_modules.config.settings import settings

logger = logging.getLogger(__name__)


@celery_app.task(name="rag_modules.tasks.document_cleanup_tasks.cleanup_document")
def cleanup_document(*, dataset_id: str, document_id: str) -> None:
    asyncio.run(_cleanup_document(dataset_id, document_id))


async def _cleanup_document(dataset_id: str, document_id: str) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from sqlalchemy import select, update

    from rag_modules.common import utcnow
    from rag_modules.db.models import DatasetIndexRecord, DocumentRecord, DocumentSegmentRecord, IndexingJobDocumentRecord
    from rag_modules.graph.factory import get_graph_store
    from rag_modules.object_storage.factory import get_object_storage
    from rag_modules.vector_stores.factory import get_vector_store

    engine = create_async_engine(settings.sqlalchemy_database_uri, poolclass=NullPool, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as session:
            document = await session.scalar(select(DocumentRecord).where(
                DocumentRecord.dataset_id == dataset_id,
                DocumentRecord.id == document_id,
                DocumentRecord.deleted_at.is_not(None),
                DocumentRecord.cleanup_completed_at.is_(None),
            ).with_for_update())
            if document is None:
                return
            document.cleanup_last_attempt_at = utcnow()
            # A worker can already be inside a remote upsert. Wait for its
            # cancellation/checkpoint (or lease recovery) before final cleanup.
            running = await session.scalar(select(IndexingJobDocumentRecord.id).where(
                IndexingJobDocumentRecord.document_id == document_id,
                IndexingJobDocumentRecord.status == "running",
            ).limit(1))
            if running is not None:
                await session.commit()
                return

            complete = True
            object_key = (document.data_source_info or {}).get("object_key")
            if object_key:
                try:
                    await get_object_storage().remove_object(object_key)
                except Exception:
                    complete = False
                    logger.warning("document_cleanup_pending store=minio document_id=%s", document_id)

            # Include building, retired and soft-deleted indexes: their remote
            # assets can outlive both activation and PostgreSQL segment rows.
            indexes = (await session.execute(select(
                DatasetIndexRecord.vector_store_provider, DatasetIndexRecord.collection_name,
            ).where(
                DatasetIndexRecord.dataset_id == dataset_id,
                DatasetIndexRecord.collection_name.is_not(None),
            ).distinct())).all()
            for provider, collection in indexes:
                if not collection:
                    continue
                try:
                    store = get_vector_store(provider)
                    await asyncio.to_thread(store.delete_document, collection, document_id)
                except Exception:
                    complete = False
                    logger.warning("document_cleanup_pending store=vector document_id=%s", document_id)
            try:
                # Disabling new graph indexing must not abandon old graph data.
                graph_store = await asyncio.to_thread(get_graph_store, for_cleanup=True)
                if graph_store is not None:
                    await asyncio.to_thread(graph_store.delete_document, dataset_id, document_id)
            except Exception:
                complete = False
                logger.warning("document_cleanup_pending store=neo4j document_id=%s", document_id)

            # Retire segments even when an independent external store is down.
            await session.execute(update(DocumentSegmentRecord).where(
                DocumentSegmentRecord.dataset_id == dataset_id,
                DocumentSegmentRecord.document_id == document_id,
                DocumentSegmentRecord.deleted_at.is_(None),
            ).values(deleted_at=utcnow()))
            if complete:
                document.cleanup_completed_at = utcnow()
            await session.commit()
    finally:
        await engine.dispose()


@celery_app.task(name="rag_modules.tasks.document_cleanup_tasks.dispatch_pending_cleanup")
def dispatch_pending_cleanup() -> list[str]:
    return asyncio.run(_dispatch_pending_cleanup())


async def _dispatch_pending_cleanup() -> list[str]:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from rag_modules.common import utcnow
    from rag_modules.db.models import DocumentRecord
    from rag_modules.tasks.publisher import get_task_publisher

    engine = create_async_engine(settings.sqlalchemy_database_uri, poolclass=NullPool, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as session:
            rows = (await session.scalars(select(DocumentRecord).where(
                DocumentRecord.deleted_at.is_not(None),
                DocumentRecord.cleanup_completed_at.is_(None),
            ).order_by(
                DocumentRecord.cleanup_last_attempt_at.asc().nullsfirst(),
                DocumentRecord.deleted_at.asc(), DocumentRecord.id.asc(),
            ).limit(100).with_for_update(skip_locked=True))).all()
            publisher = get_task_publisher()
            dispatched = []
            for document in rows:
                # Rotate failed attempts so unavailable stores cannot starve
                # later deletions. The tombstone survives broker/worker failure.
                document.cleanup_last_attempt_at = utcnow()
                try:
                    publisher.dispatch_document_cleanup(dataset_id=document.dataset_id, document_id=document.id)
                    dispatched.append(document.id)
                except Exception:
                    logger.warning("document_cleanup_dispatch_pending document_id=%s", document.id)
            await session.commit()
            return dispatched
    finally:
        await engine.dispose()
