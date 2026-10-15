from types import SimpleNamespace

from main import app
from rag_modules.api.retrieval_api import get_retrieval_service
from rag_modules.db.models import DatasetRecord


class DB:
    async def scalar(self, statement):
        return DatasetRecord(id="dataset-1", name="D", provider="p", permission="private",
                             indexing_technique="economy", created_by="u",
                             retrieval_model_config={"mode": "full_text", "top_k": 3})


def test_retrieval_stream_emits_each_hit_without_generation(client):
    class Service:
        async def retrieve(self, dataset_id, query, **options):
            assert dataset_id == "dataset-1"
            assert query == "What is it?"
            assert options["mode"] == "full_text"
            assert options["top_k"] == 3
            return SimpleNamespace(items=[SimpleNamespace(
                segment_id="s1", parent_id=None, document_id="d1", document_name="guide.txt",
                content="Original evidence", score=0.9, match_type="keyword", source_metadata={"page": 2},
            )])

    from rag_modules.api import retrieval_api
    app.dependency_overrides[get_retrieval_service] = lambda: Service()
    app.dependency_overrides[retrieval_api.get_db_session] = lambda: DB()
    response = client.post("/api/knowledge_base/dataset-1/retrieve/stream", json={"query": "What is it?"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: hit\n" in response.text
    assert '"content":"Original evidence"' in response.text
    assert '"document_name":"guide.txt"' in response.text
    assert response.text.endswith('event: done\ndata: {"count":1}\n\n')


def test_retrieval_stream_rejects_unknown_dataset(client):
    class MissingDB:
        async def scalar(self, statement):
            return None

    from rag_modules.api import retrieval_api
    app.dependency_overrides[retrieval_api.get_db_session] = lambda: MissingDB()
    response = client.post("/api/knowledge_base/missing/retrieve/stream", json={"query": "What?"})
    assert response.status_code == 404


def test_retrieval_stream_returns_empty_completion_without_generation(client):
    class Service:
        async def retrieve(self, dataset_id, query, **options):
            return SimpleNamespace(items=[])

    from rag_modules.api import retrieval_api
    app.dependency_overrides[get_retrieval_service] = lambda: Service()
    app.dependency_overrides[retrieval_api.get_db_session] = lambda: DB()
    response = client.post("/api/knowledge_base/dataset-1/retrieve/stream", json={"query": "Unknown?"})
    assert response.status_code == 200
    assert response.text == 'event: done\ndata: {"count":0}\n\n'
