from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rag_modules.db.base import Base
from rag_modules.db.models import (
    DatasetIndexRecord, DatasetRecord, DocumentRecord, DocumentSegmentRecord,
    IndexingJobRecord,
)
from rag_modules.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag_modules.services.knowledge_base_service import KnowledgeBaseService


def dataset(identifier, **kwargs):
    return DatasetRecord(
        id=identifier, name=identifier, provider="vendor", permission="only_me",
        indexing_technique="high_quality", created_by="test", **kwargs,
    )


def document(identifier, dataset_id, status="waiting", **kwargs):
    return DocumentRecord(
        id=identifier, dataset_id=dataset_id, name=f"{identifier}.txt", position=1,
        data_source_type="upload_file", created_from="api", created_by="test",
        indexing_status=status, **kwargs,
    )


def segment(identifier, dataset_id, document_id, **kwargs):
    return DocumentSegmentRecord(
        id=identifier, dataset_id=dataset_id, document_id=document_id, content="text",
        status="completed", index_type="general", embedding_status="completed", **kwargs,
    )


def job(identifier, dataset_id, status):
    return IndexingJobRecord(
        id=identifier, dataset_id=dataset_id, status=status, job_type="initial_index",
        scope="entire_dataset", indexing_technique="high_quality",
        segmentation_mode="general", process_rule={}, retrieval_config={}, created_by="test",
    )


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_status_filters_detail_and_stats_use_the_same_real_state(db):
    db.add_all([dataset(name) for name in ("empty", "waiting", "busy", "failed", "ready")])
    db.add_all([
        document("waiting-doc", "waiting"),
        document("busy-doc", "busy", "parsing"),
        document("failed-doc", "failed", "error"),
        document("ready-doc", "ready", "completed"),
        segment("ready-segment", "ready", "ready-doc"),
    ])
    await db.commit()
    service = KnowledgeBaseService(KnowledgeBaseRepository(db))
    for status, ids in [("draft", {"empty", "waiting"}), ("indexing", {"busy"}),
                        ("failed", {"failed"}), ("ready", {"ready"})]:
        items, total = await service.list_knowledge_bases(1, 20, status=status)
        assert {item.id for item in items} == ids
        assert total == len(ids)
        for item in items:
            detail = await service.get_knowledge_base(item.id)
            assert detail.status == item.status == status
    waiting = await service.get_knowledge_base("waiting")
    assert waiting.indexing_status == "not_started"
    assert await service.knowledge_base_stats() == {
        "total": 5, "ready": 1, "indexing": 1, "draft": 2, "failed": 1,
        "documents": 4, "chunks": 1,
    }


@pytest.mark.asyncio
async def test_jobs_and_active_indexes_are_respected_without_counting_staging(db):
    db.add_all([dataset("queued"), dataset("building"), dataset("active")])
    db.add_all([job("queued-job", "queued", "queued"),
                job("build-job", "building", "failed"),
                job("active-job", "active", "completed")])
    for identifier, state in [("building", "building"), ("active", "active")]:
        db.add(DatasetIndexRecord(
            id=f"{identifier}-index", dataset_id=identifier,
            created_by_job_id="build-job" if identifier == "building" else "active-job",
            index_type="high_quality", status=state, process_rule={},
            retrieval_config={}, config_hash="hash",
        ))
        db.add(document(f"{identifier}-doc", identifier, "completed"))
        db.add(segment(f"{identifier}-seg", identifier, f"{identifier}-doc",
                       dataset_index_id=f"{identifier}-index"))
    await db.commit()
    service = KnowledgeBaseService(KnowledgeBaseRepository(db))
    assert (await service.get_knowledge_base("queued")).status == "indexing"
    building = await service.get_knowledge_base("building")
    assert building.status == "failed"
    assert building.chunk_count == 0
    active = await service.get_knowledge_base("active")
    assert active.status == "ready"
    assert active.chunk_count == 1


@pytest.mark.asyncio
async def test_deleted_disabled_archived_and_unfinished_segments_are_not_usable(db):
    now = datetime.now(timezone.utc)
    db.add_all([dataset("visible"), dataset("deleted", deleted_at=now)])
    db.add_all([
        document("gone", "visible", "completed", deleted_at=now),
        document("disabled", "visible", "completed", enabled=False),
        document("archived", "visible", "completed", archived=True),
        document("unfinished", "visible", "waiting"),
        document("hidden", "deleted", "completed"),
    ])
    for identifier in ("gone", "disabled", "archived", "unfinished"):
        db.add(segment(f"{identifier}-seg", "visible", identifier))
    await db.commit()
    service = KnowledgeBaseService(KnowledgeBaseRepository(db))
    detail = await service.get_knowledge_base("visible")
    assert detail.status == "draft"
    assert detail.chunk_count == 0
    assert detail.document_count == 3
    stats = await service.knowledge_base_stats()
    assert stats["total"] == 1
    assert stats["documents"] == 3
    assert stats["chunks"] == 0


@pytest.mark.asyncio
async def test_stats_are_unbounded_and_do_not_load_dataset_entities(db):
    db.add_all([dataset(f"dataset-{i:05d}") for i in range(10001)])
    await db.commit()
    loaded = []
    event.listen(db.sync_session, "loaded_as_persistent", lambda session, obj: loaded.append(obj))
    stats = await KnowledgeBaseService(KnowledgeBaseRepository(db)).knowledge_base_stats()
    assert stats["total"] == stats["draft"] == 10001
    assert not loaded


@pytest.mark.asyncio
async def test_pagination_has_stable_tie_breaker(db):
    now = datetime.now(timezone.utc)
    db.add_all([dataset(identifier, created_at=now) for identifier in ("c", "a", "b")])
    await db.commit()
    service = KnowledgeBaseService(KnowledgeBaseRepository(db))
    first, total = await service.list_knowledge_bases(1, 2)
    second, _ = await service.list_knowledge_bases(2, 2)
    assert total == 3
    assert [item.id for item in first + second] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_rebuild_keeps_active_chunks_and_status_priority_is_consistent(db):
    db.add(dataset("rebuild"))
    current_job = job("rebuild-job", "rebuild", "failed")
    db.add(current_job)
    db.add(DatasetIndexRecord(
        id="active-index", dataset_id="rebuild", created_by_job_id="rebuild-job",
        index_type="high_quality", status="active", process_rule={},
        retrieval_config={}, config_hash="hash",
    ))
    current_document = document("active-doc", "rebuild", "completed")
    db.add(current_document)
    db.add(segment("active-seg", "rebuild", "active-doc", dataset_index_id="active-index"))
    db.add(document("failed-doc", "rebuild", "error"))
    await db.commit()
    service = KnowledgeBaseService(KnowledgeBaseRepository(db))
    assert (await service.get_knowledge_base("rebuild")).status == "ready"
    current_job.status = "running"
    current_document.indexing_status = "parsing"
    await db.commit()
    detail = await service.get_knowledge_base("rebuild")
    assert detail.status == "indexing"
    assert detail.chunk_count == 1


@pytest.mark.asyncio
async def test_only_latest_job_failure_contributes_to_status(db):
    db.add(dataset("retried"))
    old = job("old", "retried", "failed")
    old.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.add_all([old, job("new", "retried", "completed")])
    await db.commit()
    detail = await KnowledgeBaseService(KnowledgeBaseRepository(db)).get_knowledge_base("retried")
    assert detail.status == "draft"


@pytest.mark.asyncio
async def test_empty_stats_are_zero_instead_of_null(db):
    stats = await KnowledgeBaseService(KnowledgeBaseRepository(db)).knowledge_base_stats()
    assert stats == {"total": 0, "ready": 0, "indexing": 0, "draft": 0,
                     "failed": 0, "documents": 0, "chunks": 0}


@pytest.mark.asyncio
async def test_real_create_upload_detail_and_stats_api_contract(db):
    from httpx import ASGITransport, AsyncClient

    from main import app
    from rag_modules.db.session import get_db_session
    from rag_modules.object_storage.base import StoredObject
    from rag_modules.object_storage.factory import get_object_storage

    class MemoryStorage:
        def __init__(self):
            self.objects = {}

        async def put_stream(self, object_key, stream, length, content_type):
            self.objects[object_key] = stream.read(length)
            return StoredObject("graph-rag-uploads", object_key, "etag")

        async def remove_object(self, object_key):
            self.objects.pop(object_key, None)

    storage = MemoryStorage()
    app.dependency_overrides[get_db_session] = lambda: db
    app.dependency_overrides[get_object_storage] = lambda: storage
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/knowledge_base", json={
                "name": "Console test", "description": "", "permission": "only_me",
            })
            assert created.status_code == 201
            dataset_id = created.json()["id"]
            path = f"/api/knowledge_base/{dataset_id}"
            uploaded = await client.post(f"{path}/documents", files=[
                ("files", ("valid.txt", b"hello", "text/plain")),
                ("files", ("invalid.exe", b"bad", "application/octet-stream")),
            ])
            assert uploaded.status_code == 201
            assert len(uploaded.json()["documents"]) == 1
            assert len(uploaded.json()["rejected"]) == 1
            assert list(storage.objects.values()) == [b"hello"]
            detail = (await client.get(path)).json()
            assert detail["document_count"] == 1
            assert detail["status"] == "draft"
            assert detail["indexing_status"] == "not_started"
            listing = (await client.get("/api/knowledge_base/list?status=draft")).json()
            assert listing["total"] == 1
            assert listing["items"][0]["id"] == dataset_id
            stats = (await client.get("/api/knowledge_base/stats")).json()
            assert stats["draft"] == 1
            assert stats["ready"] == 0
            assert stats["documents"] == 1
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        app.dependency_overrides.pop(get_object_storage, None)
