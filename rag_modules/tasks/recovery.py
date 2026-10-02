"""Periodic recovery operations for lost indexing workers."""

from __future__ import annotations


async def recover_expired_documents(repository, *, worker_id: str = "recovery", limit: int = 100) -> list[str]:
    return await repository.recover_expired_documents(worker_id=worker_id, limit=limit)
