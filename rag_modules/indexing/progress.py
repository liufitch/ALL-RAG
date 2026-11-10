"""PostgreSQL-backed progress and cooperative cancellation."""

from __future__ import annotations

import inspect
import time

from .engine import DocumentIndexingError


class IndexingCancelled(DocumentIndexingError):
    def __init__(self) -> None:
        super().__init__("INDEXING_CANCELLED", False, "Indexing was cancelled.")


class IndexingYielded(Exception):
    """The worker's time slice ended; resume durable work without a failure."""


class DatabaseProgressReporter:
    def __init__(self, repository, *, job_document_id: str, worker_id: str, lease_seconds: int = 300,
                 deadline: float | None = None, clock=time.monotonic):
        self.repository = repository
        self.job_document_id = job_document_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.deadline = deadline
        self.clock = clock
        self.preparation_complete = True
        self.made_progress = False

    def begin_preparation(self) -> None:
        self.preparation_complete = False

    async def prepared(self, warnings) -> None:
        await self.repository.save_prepared(
            self.job_document_id, worker_id=self.worker_id,
            warnings=[{"stage": stage, "code": code} for stage, code in warnings],
        )
        self.preparation_complete = True
        if self.deadline is not None and self.clock() >= self.deadline:
            self.mark_checkpoint()

    def mark_checkpoint(self) -> None:
        self.made_progress = True

    async def pulse(self) -> None:
        await self.repository.heartbeat(
            self.job_document_id, worker_id=self.worker_id, lease_seconds=self.lease_seconds,
        )

    async def update(
        self, stage: str, progress: int, processed_segments: int,
        total_segments: int | None = None,
    ) -> None:
        result = self.repository.heartbeat(
            self.job_document_id,
            worker_id=self.worker_id,
            stage=stage,
            progress=progress,
            processed_segments=processed_segments,
            total_segments=total_segments,
            lease_seconds=self.lease_seconds,
        )
        if inspect.isawaitable(result):
            await result

    async def check_cancelled(self) -> None:
        result = self.repository.cancellation_requested(self.job_document_id, worker_id=self.worker_id)
        if inspect.isawaitable(result):
            result = await result
        if result:
            raise IndexingCancelled()
        if self.preparation_complete and self.made_progress and self.deadline is not None and self.clock() >= self.deadline:
            raise IndexingYielded()
