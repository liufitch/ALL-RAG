"""Celery maintenance tasks for pending dispatch and lease recovery."""

from __future__ import annotations

import asyncio

from .celery_app import celery_app


@celery_app.task(name="rag_modules.tasks.maintenance_tasks.recover_stale_indexing")
def recover_stale_indexing() -> list[str]:
    return asyncio.run(_recover_stale_indexing())


async def _recover_stale_indexing() -> list[str]:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from rag_modules.config.settings import settings
    from rag_modules.repositories.indexing_repository import IndexingRepository
    engine = create_async_engine(settings.sqlalchemy_database_uri, poolclass=NullPool, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as session:
            return await IndexingRepository(session).recover_expired_documents()
    finally:
        await engine.dispose()


@celery_app.task(name="rag_modules.tasks.maintenance_tasks.dispatch_pending_indexing")
def dispatch_pending_indexing() -> list[str]:
    return asyncio.run(_dispatch_pending_indexing())

async def _dispatch_pending_indexing() -> list[str]:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from rag_modules.config.settings import settings
    from rag_modules.db.models import IndexingJobDocumentRecord
    from rag_modules.tasks.publisher import get_task_publisher
    from rag_modules.common import utcnow
    engine = create_async_engine(settings.sqlalchemy_database_uri, poolclass=NullPool, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
      async with factory() as session:
        result = await session.execute(select(IndexingJobDocumentRecord).where(
            IndexingJobDocumentRecord.status.in_(("pending", "retry_wait")),
            IndexingJobDocumentRecord.available_at <= utcnow(),
        ).limit(100).with_for_update())
        rows = list(result.scalars())
        publisher = get_task_publisher()
        dispatched = []
        for row in rows:
            row.dispatch_attempts = int(row.dispatch_attempts or 0) + 1
            row.last_dispatch_at = utcnow()
            try:
                task_id = publisher.dispatch_job_document(job_document_id=row.id)
                row.celery_task_id = task_id
                row.status = "queued"
                row.last_dispatch_error = None
                dispatched.append(row.id)
            except Exception:
                row.last_dispatch_error = "BROKER_UNAVAILABLE"
        await session.commit()
        return dispatched
    finally:
      await engine.dispose()
