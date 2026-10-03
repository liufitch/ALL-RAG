"""Best-effort cleanup for soft-deleted document assets."""

from __future__ import annotations

import asyncio

from .celery_app import celery_app
from rag_modules.config.settings import settings


@celery_app.task(name="rag_modules.tasks.document_cleanup_tasks.cleanup_document")
def cleanup_document(*, dataset_id: str, document_id: str) -> None:
    asyncio.run(_cleanup_document(dataset_id, document_id))


async def _cleanup_document(dataset_id: str, document_id: str) -> None:
    # The database soft-delete remains the source of truth.  This task is
    # intentionally idempotent: missing objects and already-retired segments
    # are safe outcomes when a broker redelivers the message.
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from sqlalchemy import select, update

    from rag_modules.db.models import DocumentRecord, DocumentSegmentRecord
    from rag_modules.object_storage.factory import get_object_storage

    engine = create_async_engine(
        settings.sqlalchemy_database_uri,
        **{**settings.sqlalchemy_engine_options, "poolclass": NullPool},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as session:
            document = await session.scalar(
                select(DocumentRecord).where(
                    DocumentRecord.dataset_id == dataset_id,
                    DocumentRecord.id == document_id,
                )
            )
            if document is None:
                return
            object_key = (document.data_source_info or {}).get("object_key")
            if object_key:
                try:
                    await get_object_storage().remove_object(object_key)
                except Exception:
                    # Keep the soft-delete and let the maintenance scheduler retry.
                    return
            await session.execute(
                update(DocumentSegmentRecord).where(
                    DocumentSegmentRecord.dataset_id == dataset_id,
                    DocumentSegmentRecord.document_id == document_id,
                    DocumentSegmentRecord.deleted_at.is_(None),
                ).values(deleted_at=__import__("rag_modules.common", fromlist=["utcnow"]).utcnow())
            )
            await session.commit()
    finally:
        await engine.dispose()
