"""Task-scoped async runtime for Celery workers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from rag_modules.config.settings import settings


def run_indexing_task(job_document_id: str, *, dataset_id: str | None = None, document_id: str | None = None) -> None:
    asyncio.run(_run_indexing_task(job_document_id, dataset_id=dataset_id, document_id=document_id))


def run_finalize_indexing_job(job_id: str) -> None:
    asyncio.run(_run_finalize_indexing_job(job_id))


async def _run_indexing_task(job_document_id: str, *, dataset_id: str | None = None, document_id: str | None = None) -> None:
    engine = create_async_engine(
        settings.sqlalchemy_database_uri,
        **{**settings.sqlalchemy_engine_options, "poolclass": NullPool},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with session_factory() as session:
            from .indexing_tasks import build_task_runner

            runner = await build_task_runner(
                session,
                job_document_id=job_document_id,
                dataset_id=dataset_id,
                document_id=document_id,
            )
            if runner is not None:
                context_id = job_document_id
                if dataset_id and document_id:
                    from rag_modules.repositories.indexing_repository import IndexingRepository
                    resolved = await IndexingRepository(session).ensure_compatibility_job_document(dataset_id, document_id)
                    context_id = resolved or job_document_id
                await runner.run(context_id)
    finally:
        await engine.dispose()


async def _run_finalize_indexing_job(job_id: str) -> None:
    engine = create_async_engine(
        settings.sqlalchemy_database_uri,
        **{**settings.sqlalchemy_engine_options, "poolclass": NullPool},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with session_factory() as session:
            from rag_modules.repositories.indexing_repository import IndexingRepository

            await IndexingRepository(session).finalize_indexing_job(job_id)
    finally:
        await engine.dispose()
