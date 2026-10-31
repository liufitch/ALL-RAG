import pytest
from types import SimpleNamespace

from rag_modules.indexing.engine import DocumentIndexingEngine
from rag_modules.graph.indexer import make_graph_indexer
from tests.unit.indexing.test_document_engine import make_engine, economy_command, RecordingProgress


@pytest.mark.asyncio
async def test_graph_index_runs_after_segments_are_staged():
    engine, deps = make_engine(forbidden_high_quality=True)
    calls = []

    class GraphIndexer:
        async def index(self, command, records, progress):
            calls.append((command.staging.dataset_index_id, [record.id for record in records]))

    engine._graph_indexer = GraphIndexer()
    await engine.run(economy_command(), RecordingProgress())
    assert calls == [("index-1", [record.id for record in deps.repository.records])]


@pytest.mark.asyncio
async def test_graph_index_reports_segment_progress_and_checks_cancellation():
    from rag_modules.graph.extraction import GraphExtraction
    from rag_modules.graph.indexer import GraphIndexer

    events = []

    class Extractor:
        model = "chat"

        async def extract(self, text):
            events.append(("extract", text))
            return GraphExtraction((), ())

        async def aclose(self):
            pass

    class Store:
        def upsert_segment(self, *args):
            events.append(("write", args[3]))

    class Progress:
        async def update(self, stage, progress, processed, total=None):
            events.append(("progress", stage, progress, processed, total))

        async def check_cancelled(self):
            events.append(("check",))

    records = [
        SimpleNamespace(id="s1", content="one", dataset_id="d", dataset_index_id="i", document_id="doc"),
        SimpleNamespace(id="s2", content="two", dataset_id="d", dataset_index_id="i", document_id="doc"),
    ]
    await GraphIndexer(Store(), Extractor()).index(SimpleNamespace(), records, Progress())

    progress = [event for event in events if event[0] == "progress"]
    assert progress == [
        ("progress", "graph-extract", 94, 1, 2),
        ("progress", "graph-extract", 98, 2, 2),
    ]
    assert events.count(("check",)) == 4


def test_enabled_graph_indexer_does_not_connect_before_job_is_claimed(monkeypatch):
    from rag_modules.config.settings import settings
    from rag_modules.graph import factory

    monkeypatch.setattr(settings.graph, "enabled", True)
    monkeypatch.setattr(settings.graph, "extraction_model", "chat")
    monkeypatch.setattr(factory, "get_graph_store", lambda: pytest.fail("connected before claim"))
    assert make_graph_indexer() is not None
