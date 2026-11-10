from contextlib import asynccontextmanager
from io import BytesIO

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from main import create_app
from rag_modules.api.retrieval_api import get_retrieval_service
from rag_modules.common import utcnow
from rag_modules.db.base import Base
from rag_modules.db.models import (
    DatasetIndexRecord, DatasetRecord, DocumentRecord, DocumentRevisionRecord, DocumentSegmentRecord, IndexingJobRecord,
)
from rag_modules.db.session import get_db_session
from rag_modules.object_storage.factory import get_object_storage
from rag_modules.repositories.document_repository import DocumentRepository
from rag_modules.retrieval import RetrievalService
from rag_modules.tasks.publisher import get_task_publisher


class SourceStorage:
    @asynccontextmanager
    async def get_stream(self, object_key):
        yield BytesIO(b"alpha original evidence")


class Publisher:
    def dispatch_job_document(self, **kwargs):
        return "task"

    def dispatch_document_cleanup(self, **kwargs):
        return "cleanup"


@pytest_asyncio.fixture
async def dataset_client(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'access.db'}")
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    for index in metadata.tables["dataset_indexes"].indexes:
        if index.name == "uq_dataset_indexes_one_active":
            index.dialect_options["sqlite"]["where"] = text("status = 'active' AND deleted_at IS NULL")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([
            DatasetRecord(id="dataset", name="D", provider="vendor", permission="only_me", indexing_technique="economy", created_by="owner"),
            DocumentRecord(id="document", dataset_id="dataset", name="source.txt", data_source_type="upload_file", created_from="api", created_by="owner", indexing_status="completed", data_source_info={"object_key": "source.txt"}),
            IndexingJobRecord(id="job", dataset_id="dataset", job_type="initial_index", scope="selected_documents", status="completed", indexing_technique="economy", segmentation_mode="general", process_rule={}, retrieval_config={}, created_by="owner"),
            DatasetIndexRecord(id="index", dataset_id="dataset", created_by_job_id="job", index_type="economy", status="active", process_rule={}, retrieval_config={}, config_hash="hash"),
            DocumentSegmentRecord(id="segment", dataset_id="dataset", document_id="document", dataset_index_id="index", content="alpha original evidence", keywords=["alpha"], status="completed", index_type="general", embedding_status="not_required", position=0),
        ])
        await session.commit()

    app = create_app()

    async def database():
        async with factory() as session:
            yield session

    async def retrieval():
        async with factory() as session:
            yield RetrievalService(session)

    app.dependency_overrides[get_db_session] = database
    app.dependency_overrides[get_retrieval_service] = retrieval
    app.dependency_overrides[get_object_storage] = SourceStorage
    app.dependency_overrides[get_task_publisher] = Publisher
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, factory, app
    finally:
        await engine.dispose()


CONFIG = {"document_ids": ["document"], "indexing_technique": "economy", "segmentation": {"mode": "general"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "suffix", "payload"), [
    ("get", "", None),
    ("post", "/retrieve", {"query": "alpha", "mode": "full_text"}),
    ("post", "/retrieve/stream", {"query": "alpha", "mode": "full_text"}),
    ("get", "/documents", None),
    ("get", "/documents/document/download", None),
    ("get", "/documents/document/segments", None),
    ("get", "/documents/document/jobs/job", None),
    ("patch", "/documents/document", {"name": "renamed.txt"}),
    ("post", "/documents/document/enable", None),
    ("post", "/documents/document/disable", None),
    ("post", "/documents/document/archive", None),
    ("post", "/documents/document/restore", None),
    ("post", "/documents/document/reindex", None),
    ("patch", "/documents/document/segments", {"base_version": 0, "segments": [{"id": "segment", "position": 0, "content": "changed", "index_type": "general"}]}),
    ("delete", "/documents/document", None),
    ("get", "/indexing/jobs", None),
    ("get", "/indexing/jobs/job", None),
    ("post", "/indexing/jobs/job/retry", None),
    ("post", "/indexing/jobs/job/cancel", None),
    ("post", "/indexing/jobs", CONFIG),
    ("post", "/indexing/configure", CONFIG),
    ("get", "/settings", None),
    ("patch", "/settings", {"name": "renamed"}),
    ("post", "/rebuild", None),
    ("get", "/export", None),
])
async def test_deleted_dataset_rejects_content_and_actions(dataset_client, method, suffix, payload):
    client, _, _ = dataset_client
    base = "/api/knowledge_base/dataset"
    assert (await client.post(base + "/retrieve", json={"query": "alpha", "mode": "full_text"})).json()["items"][0]["content"] == "alpha original evidence"
    assert (await client.get(base + "/documents/document/download")).content == b"alpha original evidence"
    assert (await client.delete(base)).status_code == 204

    response = await client.request(method, base + suffix, **({"json": payload} if payload is not None else {}))

    assert response.status_code == 404, response.text
    assert "alpha original evidence" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["document", "retrieval"])
async def test_historically_deleted_dataset_is_filtered_by_document_and_retrieval_queries(dataset_client, query):
    _, factory, _ = dataset_client
    async with factory() as session:
        dataset = await session.get(DatasetRecord, "dataset")
        dataset.deleted_at = utcnow()
        await session.commit()

        if query == "document":
            assert await DocumentRepository(session).get_active_document("dataset", "document") is None
        else:
            result = await RetrievalService(session).retrieve("dataset", "alpha", mode="full_text")
            assert result.items == []


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["retrieve", "retrieve/stream"])
async def test_deleted_dataset_is_rejected_before_connecting_to_retrieval_providers(dataset_client, monkeypatch, endpoint):
    from rag_modules.api import retrieval_api

    client, _, app = dataset_client
    assert (await client.delete("/api/knowledge_base/dataset")).status_code == 204
    app.dependency_overrides.pop(get_retrieval_service)

    def unavailable():
        raise AssertionError("Deleted dataset must be rejected before contacting a retrieval provider")

    monkeypatch.setattr(retrieval_api, "get_graph_store", unavailable)
    monkeypatch.setattr(retrieval_api, "get_vector_store", unavailable)

    response = await client.post(f"/api/knowledge_base/dataset/{endpoint}", json={"query": "alpha"})
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["stale_revision", "no_active_index"])
async def test_deleted_dataset_revision_action_checks_parent_before_revision_state(dataset_client, state):
    client, factory, _ = dataset_client
    async with factory() as session:
        if state == "stale_revision":
            session.add(DocumentRevisionRecord(
                id="revision", dataset_id="dataset", document_id="document", version=1,
                segments={"items": []}, created_by="owner", status="completed", indexing_job_id="job",
            ))
        else:
            (await session.get(DatasetIndexRecord, "index")).status = "retired"
        await session.commit()
    assert (await client.delete("/api/knowledge_base/dataset")).status_code == 204

    response = await client.patch("/api/knowledge_base/dataset/documents/document/segments", json={
        "base_version": 0,
        "segments": [{"id": "segment", "position": 0, "content": "changed", "index_type": "general"}],
    })
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_active_dataset_job_creation_returns_persisted_job_and_rejects_overlapping_build(dataset_client):
    client, factory, _ = dataset_client
    async with factory() as session:
        (await session.get(DatasetIndexRecord, "index")).status = "retired"
        await session.commit()

    response = await client.post("/api/knowledge_base/dataset/indexing/jobs", json=CONFIG)

    assert response.status_code == 202, response.text
    job = response.json()["job"]
    assert job["job_type"] == "initial_index"
    assert [document["document_id"] for document in job["documents"]] == ["document"]
    assert (await client.get(f"/api/knowledge_base/dataset/indexing/jobs/{job['id']}")).status_code == 200
    assert (await client.post("/api/knowledge_base/dataset/indexing/configure", json=CONFIG)).status_code == 409
