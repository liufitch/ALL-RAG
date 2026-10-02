"""PostgreSQL-backed progress and cooperative cancellation."""

from __future__ import annotations

import inspect

from .engine import DocumentIndexingError


class IndexingCancelled(DocumentIndexingError):
    def __init__(self) -> None:
        super().__init__("INDEXING_CANCELLED", False, "Indexing was cancelled.")


class DatabaseProgressReporter:
    def __init__(self, repository, *, job_document_id: str, worker_id: str, lease_seconds: int = 300):
        self.repository = repository
        self.job_document_id = job_document_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    async def update(self, stage: str, progress: int, processed_segments: int) -> None:
        result = self.repository.heartbeat(
            self.job_document_id,
            worker_id=self.worker_id,
            stage=stage,
            progress=progress,
            processed_segments=processed_segments,
            lease_seconds=self.lease_seconds,
        )
        if inspect.isawaitable(result):
            await result

    async def check_cancelled(self) -> None:
        result = self.repository.cancellation_requested(self.job_document_id)
        if inspect.isawaitable(result):
            result = await result
        if result:
            raise IndexingCancelled()
