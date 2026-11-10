import asyncio
from types import SimpleNamespace

import pytest

from rag_modules.graph.extraction import GraphExtraction
from rag_modules.graph.indexer import GraphIndexer


class CheckpointStore:
    def __init__(self):
        self.completed = {}

    def completed_segments(self, index_id, fingerprints):
        return {identifier for identifier, fingerprint in fingerprints.items()
                if self.completed.get((index_id, identifier)) == fingerprint}

    def upsert_segment(self, dataset_id, index_id, document_id, segment_id, extraction, *, fingerprint=None):
        self.completed[index_id, segment_id] = fingerprint


class Extractor:
    model = "model"

    def __init__(self):
        self.calls = []
        self.active = self.peak = 0
        self.closed = False

    async def extract(self, text):
        self.calls.append(text)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(.002)
            return GraphExtraction((), ())
        finally:
            self.active -= 1

    async def aclose(self):
        assert self.active == 0
        self.closed = True


class Progress:
    def __init__(self, stop_at=None):
        self.counts = []
        self.stop_at = stop_at

    async def update(self, stage, percent, processed, total=None):
        self.counts.append(processed)
        if processed == self.stop_at:
            raise asyncio.CancelledError()

    async def check_cancelled(self):
        pass


def records(count=9):
    return [SimpleNamespace(id=str(i), content=f"segment {i}", dataset_id="dataset",
                            dataset_index_id="index", document_id="document") for i in range(count)]


@pytest.mark.asyncio
async def test_graph_extraction_is_bounded_and_resume_skips_successful_segments():
    store = CheckpointStore()
    first = Extractor()
    with pytest.raises(asyncio.CancelledError):
        await GraphIndexer(store, first, max_concurrency=3).index(None, records(), Progress(stop_at=3))
    assert first.peak == 3
    assert first.closed
    assert len(store.completed) == 3

    resumed = Extractor()
    progress = Progress()
    await GraphIndexer(store, resumed, max_concurrency=3).index(None, records(), progress)

    assert len(resumed.calls) == 6
    assert len(store.completed) == 9
    assert progress.counts[-1] == 9
    assert progress.counts == sorted(progress.counts)
    assert resumed.closed


@pytest.mark.asyncio
async def test_graph_checkpoint_is_invalidated_when_extraction_configuration_changes():
    store = CheckpointStore()
    await GraphIndexer(store, Extractor(), signature="schema-v1").index(None, records(2), Progress())
    changed = Extractor()
    await GraphIndexer(store, changed, signature="schema-v2").index(None, records(2), Progress())
    assert len(changed.calls) == 2
