import pytest

from rag_modules.config.settings import VectorStoreSettings
from rag_modules.vector_stores.base import VectorValidationError
from rag_modules.vector_stores.milvus import MilvusVectorStore


class SearchClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def test_search_scopes_candidates_and_returns_scores():
    client = SearchClient([[{"id": "segment-1", "distance": 0.8}]])
    store = MilvusVectorStore(
        config=VectorStoreSettings(), client_factory=lambda: client
    )
    hits = store.search("collection", (0.1, 0.2), "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", 4)
    assert [(hit.id, hit.score) for hit in hits] == [("segment-1", 0.8)]
    assert client.calls[0]["filter"] == (
        'dataset_id == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" and dataset_index_id == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"'
    )
    assert client.calls[0]["limit"] == 4


def test_search_rejects_invalid_vector_before_calling_client():
    client = SearchClient([])
    store = MilvusVectorStore(
        config=VectorStoreSettings(), client_factory=lambda: client
    )
    with pytest.raises(VectorValidationError):
        store.search("collection", (float("nan"),), "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", 4)
    assert client.calls == []
