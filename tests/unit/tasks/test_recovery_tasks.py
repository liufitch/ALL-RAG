from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_dispatcher_requeues_due_retries_and_keeps_future_retries_waiting(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from rag_modules.common import utcnow
    from rag_modules.db.base import Base
    from rag_modules.db.models import IndexingJobDocumentRecord
    from rag_modules.tasks.maintenance_tasks import _dispatch_pending_indexing

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retry-dispatch.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        for identifier, status, delay in [
            ("pending", "pending", -1),
            ("due-retry", "retry_wait", -1),
            ("future-retry", "retry_wait", 600),
        ]:
            session.add(IndexingJobDocumentRecord(
                id=identifier, job_id="job-1", document_id=identifier,
                status=status, available_at=utcnow() + timedelta(seconds=delay),
            ))
        await session.commit()
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", lambda *a, **kw: engine)
    monkeypatch.setattr("rag_modules.tasks.publisher.get_task_publisher", lambda: SimpleNamespace(
        dispatch_job_document=lambda **kw: "task-" + kw["job_document_id"],
    ))

    await _dispatch_pending_indexing()

    async with factory() as session:
        rows = {r.id: r for r in (await session.scalars(select(IndexingJobDocumentRecord))).all()}
        assert rows["pending"].status == "queued"
        assert rows["due-retry"].status == "queued"
        assert rows["due-retry"].celery_task_id == "task-due-retry"
        assert rows["due-retry"].dispatch_attempts == 1
        assert rows["future-retry"].status == "retry_wait"
        assert rows["future-retry"].dispatch_attempts == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_running_document_is_requeued_by_recovery():
    from rag_modules.tasks.recovery import recover_expired_documents

    class Repo:
        async def recover_expired_documents(self, **kwargs):
            return ["jd-1"]

    assert await recover_expired_documents(Repo(), worker_id="beat") == ["jd-1"]
