import pytest

from rag_modules.indexing.engine import DocumentIndexingEngine
from rag_modules.graph.indexer import make_graph_indexer
from tests.unit.indexing.test_document_engine import make_engine, economy_command, RecordingProgress


@pytest.mark.asyncio
async def test_graph_index_runs_after_segments_are_staged():
    engine, deps = make_engine(forbidden_high_quality=True)
    calls = []

    class GraphIndexer:
        async def index(self, command, records):
            calls.append((command.staging.dataset_index_id, [record.id for record in records]))

    engine._graph_indexer = GraphIndexer()
    await engine.run(economy_command(), RecordingProgress())
    assert calls == [("index-1", [record.id for record in deps.repository.records])]


def test_enabled_graph_indexer_does_not_connect_before_job_is_claimed(monkeypatch):
    from rag_modules.config.settings import settings
    from rag_modules.graph import factory

    monkeypatch.setattr(settings.graph, "enabled", True)
    monkeypatch.setattr(settings.graph, "extraction_model", "chat")
    monkeypatch.setattr(factory, "get_graph_store", lambda: pytest.fail("connected before claim"))
    assert make_graph_indexer() is not None
