from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest


@dataclass
class FakeRow:
    id: str = "jd-1"
    job_id: str = "job-1"
    document_id: str = "doc-1"
    status: str = "pending"
    attempt: int = 0
    max_attempts: int = 3
    progress: int = 0
    processed_segments: int = 0
    total_segments: int = 0
    embedded_segments: int = 0
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    cancel_requested: bool = False
    error_code: str | None = None
    error: str | None = None


class FakeRepository:
    def __init__(self, row: FakeRow | None = None):
        self.row = row or FakeRow()
        self.calls: list[tuple[str, object]] = []

    async def claim_job_document(self, job_document_id, worker_id, lease_seconds, **kwargs):
        self.calls.append(("claim", job_document_id))
        if self.row.status == "running" and self.row.lease_expires_at and self.row.lease_expires_at > datetime.now(timezone.utc):
            return None
        self.row.status = "running"
        self.row.worker_id = worker_id
        self.row.attempt += 1
        self.row.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        return self.row

    async def heartbeat(self, *args, **kwargs):
        self.calls.append(("heartbeat", kwargs.get("stage")))

    async def complete_job_document(self, *args, **kwargs):
        self.calls.append(("complete", kwargs))
        self.row.status = "completed"

    async def fail_job_document(self, *args, **kwargs):
        self.calls.append(("fail", kwargs))
        self.row.status = "retry_wait" if kwargs["retryable"] else "failed"
        self.row.error_code = kwargs["error_code"]
        self.row.error = kwargs["error"]

    async def cancel_job_document(self, *args, **kwargs):
        self.calls.append(("cancel", kwargs))
        self.row.status = "cancelled"

    async def refresh_job_summary(self, *args, **kwargs):
        self.calls.append(("refresh", kwargs))

    async def cancellation_requested(self, *args, **kwargs):
        return self.row.cancel_requested


class FakeEngine:
    async def run(self, command, progress):
        await progress.update("parse", 15, 0)
        await progress.check_cancelled()
        return type("Result", (), {"total_segments": 2, "total_indexable_segments": 2, "vector_count": 2, "warnings": ()})()


@pytest.mark.asyncio
async def test_index_document_claims_runs_engine_and_persists_completion():
    from rag_modules.tasks.indexing_tasks import IndexingTaskRunner

    repo = FakeRepository()
    runner = IndexingTaskRunner(repository=repo, engine=FakeEngine(), command_factory=lambda row: object(), worker_id="worker-1")

    await runner.run("jd-1")

    assert repo.row.status == "completed"
    assert [name for name, _ in repo.calls] == ["claim", "heartbeat", "complete", "refresh"]


@pytest.mark.asyncio
async def test_duplicate_delivery_does_not_run_engine_while_lease_is_active():
    from rag_modules.tasks.indexing_tasks import IndexingTaskRunner

    repo = FakeRepository(FakeRow(status="running", worker_id="other", lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)))
    engine = FakeEngine()
    runner = IndexingTaskRunner(repository=repo, engine=engine, command_factory=lambda row: object(), worker_id="worker-1")

    await runner.run("jd-1")

    assert repo.row.status == "running"
    assert [name for name, _ in repo.calls] == ["claim"]


@pytest.mark.asyncio
async def test_retryable_engine_error_sets_retry_wait_with_safe_error():
    from rag_modules.indexing.engine import DocumentIndexingError
    from rag_modules.tasks.indexing_tasks import IndexingTaskRunner

    class FailingEngine:
        async def run(self, command, progress):
            raise DocumentIndexingError("VECTOR_TEMPORARY", True, "Vector service is temporarily unavailable.")

    repo = FakeRepository()
    runner = IndexingTaskRunner(repository=repo, engine=FailingEngine(), command_factory=lambda row: object(), worker_id="worker-1")

    await runner.run("jd-1")

    assert repo.row.status == "retry_wait"
    assert repo.row.error_code == "VECTOR_TEMPORARY"
    assert repo.row.error == "Vector service is temporarily unavailable."


@pytest.mark.asyncio
async def test_cancel_requested_stops_engine_and_marks_cancelled():
    from rag_modules.tasks.indexing_tasks import IndexingTaskRunner

    repo = FakeRepository(FakeRow(cancel_requested=True))
    runner = IndexingTaskRunner(repository=repo, engine=FakeEngine(), command_factory=lambda row: object(), worker_id="worker-1")

    await runner.run("jd-1")

    assert repo.row.status == "cancelled"
    assert any(name == "cancel" for name, _ in repo.calls)
