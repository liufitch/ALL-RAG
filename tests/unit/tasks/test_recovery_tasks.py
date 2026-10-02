from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_expired_running_document_is_requeued_by_recovery():
    from rag_modules.tasks.recovery import recover_expired_documents

    class Repo:
        async def recover_expired_documents(self, **kwargs):
            return ["jd-1"]

    assert await recover_expired_documents(Repo(), worker_id="beat") == ["jd-1"]
