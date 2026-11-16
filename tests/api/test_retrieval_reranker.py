"""Exercise the real API dependency and retrieval path with external stores replaced."""
from contextlib import aclosing

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rag_modules.api import retrieval_api
from rag_modules.db.base import Base
from rag_modules.db.models import DatasetIndexRecord, DatasetRecord, DocumentRecord, DocumentSegmentRecord


@pytest_asyncio.fixture
async def retrieval_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add_all([
            DatasetRecord(id="dataset", name="D", provider="p", permission="private", indexing_technique="economy", created_by="u"),
            DatasetIndexRecord(id="index", dataset_id="dataset", created_by_job_id="job", index_type="economy", status="active", retrieval_config={}, process_rule={}, config_hash="hash"),
            DocumentRecord(id="doc", dataset_id="dataset", name="guide.txt", data_source_type="upload", created_from="api", created_by="u", indexing_status="completed"),
            DocumentSegmentRecord(id="a", dataset_id="dataset", document_id="doc", dataset_index_id="index", content="less relevant", keywords=["graph"], status="completed", index_type="general", embedding_status="not_required"),
            DocumentSegmentRecord(id="b", dataset_id="dataset", document_id="doc", dataset_index_id="index", content="most relevant", keywords=["graph"], status="completed", index_type="general", embedding_status="not_required"),
        ])
        await db.commit()
        yield db
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_real_dependency_reranks_only_when_requested(retrieval_db, monkeypatch, enabled):
    requests = []
    original_post = httpx.AsyncClient.post

    async def post(client, url, **kwargs):
        if str(url).endswith("/rerank"):
            requests.append(kwargs["json"])
            return httpx.Response(200, json={"results": [
                {"index": 1, "relevance_score": .9}, {"index": 0, "relevance_score": .1},
            ]}, request=httpx.Request("POST", url))
        return await original_post(client, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr(retrieval_api, "get_graph_store", lambda: None)
    monkeypatch.setattr(retrieval_api, "get_vector_store", lambda: None)
    dependency = retrieval_api.get_retrieval_service(db=retrieval_db, _dataset=None)
    async with aclosing(dependency):
        service = await anext(dependency)
        assert service.reranker is not None
        result = await service.retrieve("dataset", "graph", mode="full_text", score_threshold=0, top_k=1, rerank_enabled=enabled)
    assert service.reranker._http.is_closed
    assert [item.segment_id for item in result.items] == (["b"] if enabled else ["a"])
    assert len(requests) == (1 if enabled else 0)
    if enabled:
        assert requests[0]["documents"] == ["less relevant", "most relevant"]
        assert {item["segment_id"]: item["after_rank"] for item in result.rerank_trace} == {"a": 2, "b": 1}


@pytest.mark.asyncio
async def test_rerank_failure_preserves_base_recall(retrieval_db, monkeypatch):
    async def fail(*args, **kwargs):
        raise httpx.ConnectError("provider unavailable")
    monkeypatch.setattr(httpx.AsyncClient, "post", fail)
    monkeypatch.setattr(retrieval_api, "get_graph_store", lambda: None)
    monkeypatch.setattr(retrieval_api, "get_vector_store", lambda: None)
    dependency = retrieval_api.get_retrieval_service(db=retrieval_db, _dataset=None)
    async with aclosing(dependency):
        service = await anext(dependency)
        result = await service.retrieve("dataset", "graph", mode="full_text", score_threshold=0, rerank_enabled=True)
    assert [item.segment_id for item in result.items] == ["a", "b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["retrieve", "retrieve/stream"])
async def test_normal_and_stream_endpoints_use_saved_rerank_with_explicit_override(retrieval_db, monkeypatch, endpoint):
    from main import app
    dataset = await retrieval_db.get(DatasetRecord, "dataset")
    dataset.retrieval_model_config = {"mode": "full_text", "rerank_enabled": True, "score_threshold": 0, "top_k": 1}
    await retrieval_db.commit()
    requests = []
    original_post = httpx.AsyncClient.post

    async def post(client, url, **kwargs):
        if str(url).endswith("/rerank"):
            requests.append(kwargs["json"])
            return httpx.Response(200, json={"results": [
                {"index": 1, "relevance_score": .9}, {"index": 0, "relevance_score": .1},
            ]}, request=httpx.Request("POST", url))
        return await original_post(client, url, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr(retrieval_api, "get_graph_store", lambda: None)
    monkeypatch.setattr(retrieval_api, "get_vector_store", lambda: None)
    app.dependency_overrides[retrieval_api.get_db_session] = lambda: retrieval_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test") as client:
            for override, expected in [(None, "b"), (False, "a")]:
                payload = {"query": "graph"}
                if override is not None:
                    payload["rerank_enabled"] = override
                response = await client.post(f"/api/knowledge_base/dataset/{endpoint}", json=payload)
                assert response.status_code == 200
                if endpoint == "retrieve":
                    assert response.json()["items"][0]["segment_id"] == expected
                else:
                    import json
                    hit = next(line for line in response.text.splitlines() if line.startswith("data: "))
                    assert json.loads(hit[6:])["segment_id"] == expected
        assert len(requests) == 1
    finally:
        app.dependency_overrides.clear()
