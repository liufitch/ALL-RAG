import asyncio
from dataclasses import replace
import threading

import pytest

from rag_modules.embeddings import EmbeddingBatch
from tests.unit.indexing.test_document_engine import (
    RecordingProgress, high_quality_command, make_engine, source_blocks,
)


@pytest.mark.asyncio
async def test_embedding_requests_overlap_with_bounded_memory_and_serial_persistence():
    engine, deps = make_engine(blocks=source_blocks(13))
    active = peak = 0

    async def embed(model, texts):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            # Different completion order must preserve each batch's segment IDs.
            await asyncio.sleep(.005 if "0 " in texts[0] else .001)
            return EmbeddingBatch(tuple((float(text.split()[1]), 1., 2.) for text in texts), 3)
        finally:
            active -= 1

    deps.embedding.embed = embed
    command = replace(high_quality_command(batch_size=2), embedding_concurrency=3)
    result = await engine.run(command, RecordingProgress())

    assert peak == 3
    assert active == 0
    assert result.vector_count == 13
    assert len(deps.vector_store.entities) == 13
    assert {entity.id: entity.embedding[0] for entity in deps.vector_store.entities} == {
        f"rec-{i}": float(i) for i in range(13)
    }
    assert all(row.embedding_status == "completed" for row in deps.repository.records)


@pytest.mark.asyncio
async def test_embedding_cancel_drains_inflight_requests_before_returning():
    engine, deps = make_engine(blocks=source_blocks(12))
    ready = asyncio.Event()
    active = 0

    async def embed(model, texts):
        nonlocal active
        active += 1
        if active == 3:
            ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    deps.embedding.embed = embed
    command = replace(high_quality_command(batch_size=1), embedding_concurrency=3)
    task = asyncio.create_task(engine.run(command, RecordingProgress()))
    try:
        await asyncio.wait_for(ready.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0
        assert deps.vector_store.batches == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_slow_embedding_keeps_lease_alive_and_observes_cancellation():
    engine, deps = make_engine(blocks=source_blocks(1))

    async def embed(model, texts):
        await asyncio.Event().wait()

    class Progress(RecordingProgress):
        def __init__(self):
            super().__init__()
            self.pulses = 0

        async def pulse(self):
            self.pulses += 1

        async def check_cancelled(self):
            if self.pulses >= 2:
                raise asyncio.CancelledError()

    deps.embedding.embed = embed
    progress = Progress()
    command = replace(high_quality_command(), heartbeat_interval_seconds=.01)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(engine.run(command, progress), timeout=1)
    assert progress.pulses >= 2
    assert deps.vector_store.batches == []


@pytest.mark.asyncio
async def test_cancelled_synchronous_preparation_keeps_lease_until_thread_releases_resources():
    from rag_modules.indexing.concurrency import wait_external
    from rag_modules.indexing.engine import DocumentIndexingEngine

    started, release = threading.Event(), threading.Event()

    def parse():
        started.set()
        release.wait(timeout=2)

    class Progress:
        pulses = 0

        async def check_cancelled(self):
            pass

        async def pulse(self):
            self.pulses += 1

    progress = Progress()
    task = asyncio.create_task(wait_external(DocumentIndexingEngine._run_sync(parse), progress=progress, heartbeat_interval=.005))
    try:
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        before = progress.pulses
        await asyncio.sleep(.025)
        assert not task.done()
        assert progress.pulses > before
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
