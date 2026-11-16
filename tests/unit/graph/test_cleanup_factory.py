from types import SimpleNamespace

from rag_modules.config.settings import settings
from rag_modules.graph.factory import get_graph_store


def test_cleanup_can_remove_historical_graph_after_graph_feature_is_disabled(monkeypatch):
    from neo4j import GraphDatabase
    calls = []
    monkeypatch.setattr(settings.graph, "enabled", False)
    monkeypatch.setattr(GraphDatabase, "driver", lambda *a, **kw: calls.append((a, kw)) or SimpleNamespace())
    get_graph_store.cache_clear()
    try:
        assert get_graph_store() is None
        assert calls == []
        assert get_graph_store(for_cleanup=True) is not None
        assert len(calls) == 1
    finally:
        get_graph_store.cache_clear()
