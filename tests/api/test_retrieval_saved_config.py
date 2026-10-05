from types import SimpleNamespace

import pytest

from main import app
from rag_modules.api.retrieval_api import get_retrieval_service
from rag_modules.db.models import DatasetRecord


class Service:
    def __init__(self):
        self.args = None

    async def retrieve(self, *args, **kwargs):
        self.args = kwargs
        return SimpleNamespace(items=[], rerank_trace=[])


@pytest.fixture
def retrieval_client(client):
    service = Service()
    app.dependency_overrides[get_retrieval_service] = lambda: service
    yield client, service
    app.dependency_overrides.clear()


def test_retrieve_uses_saved_dataset_config_when_fields_omitted(retrieval_client, monkeypatch):
    client, service = retrieval_client

    class Result:
        def scalar_one_or_none(self):
            return DatasetRecord(
                id="dataset-1", name="D", provider="p", permission="private",
                indexing_technique="high_quality", created_by="u",
                retrieval_model_config={"mode": "vector", "top_k": 9, "score_threshold": 0.8,
                                       "semantic_weight": 1, "keyword_weight": 0, "rerank_enabled": True},
            )

    class DB:
        async def scalar(self, statement):
            return Result().scalar_one_or_none()

        async def execute(self, statement):
            return Result()

    from rag_modules.api import retrieval_api
    app.dependency_overrides[retrieval_api.get_db_session] = lambda: DB()
    response = client.post("/api/knowledge_base/dataset-1/retrieve", json={"query": "graph"})

    assert response.status_code == 200
    assert service.args["mode"] == "vector"
    assert service.args["top_k"] == 9
    assert service.args["rerank_enabled"] is True
