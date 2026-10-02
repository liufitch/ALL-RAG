"""Celery maintenance tasks for pending dispatch and lease recovery."""

from __future__ import annotations

import asyncio

from .celery_app import celery_app


@celery_app.task(name="rag_modules.tasks.maintenance_tasks.recover_stale_indexing")
def recover_stale_indexing() -> list[str]:
    return asyncio.run(_recover_stale_indexing())


async def _recover_stale_indexing() -> list[str]:
    from rag_modules.db.session import SessionLocal
    from rag_modules.repositories.indexing_repository import IndexingRepository

    async with SessionLocal() as session:
        return await IndexingRepository(session).recover_expired_documents()
