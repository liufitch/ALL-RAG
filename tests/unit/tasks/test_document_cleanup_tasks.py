from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rag_modules.common import utcnow
from rag_modules.db.base import Base
from rag_modules.db.models import DatasetIndexRecord, DatasetRecord, DocumentRecord, DocumentSegmentRecord, IndexingJobDocumentRecord, IndexingJobRecord
from rag_modules.tasks import document_cleanup_tasks as tasks


@pytest_asyncio.fixture
async def cleanup_env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cleanup.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DROP INDEX uq_dataset_indexes_one_active"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add_all([
            DatasetRecord(id="dataset", name="D", provider="p", permission="private", indexing_technique="high_quality", created_by="u"),
            IndexingJobRecord(id="job", dataset_id="dataset", job_type="initial_index", scope="selected_documents", status="running", indexing_technique="high_quality", segmentation_mode="general", process_rule={}, retrieval_config={}, created_by="u"),
            DocumentRecord(id="doc", dataset_id="dataset", name="file.txt", data_source_type="upload", data_source_info={"object_key": "source"}, created_from="api", created_by="u", indexing_status="completed", deleted_at=utcnow()),
            DocumentRecord(id="other", dataset_id="dataset", name="other.txt", data_source_type="upload", data_source_info={"object_key": "other-source"}, created_from="api", created_by="u", indexing_status="completed"),
            *[DatasetIndexRecord(id=name, dataset_id="dataset", created_by_job_id="job", index_type="high_quality", status=status, vector_store_provider="milvus", collection_name=name, retrieval_config={}, process_rule={}, config_hash=name, deleted_at=utcnow() if name == "old" else None) for name, status in [("active", "active"), ("old", "retired"), ("building", "building")]],
            *[DocumentSegmentRecord(id=name, dataset_id="dataset", document_id=doc, dataset_index_id=index, content="text", status="completed", index_type="general", embedding_status="completed") for name, doc, index in [("s1", "doc", "active"), ("s2", "doc", "old"), ("s3", "other", "active")]],
        ])
        await db.commit()
    state = SimpleNamespace(objects={"source", "other-source"}, vectors={(i, d) for i in ["active", "old", "building"] for d in ["doc", "other"]}, graph={"doc", "other"}, fail=set(), calls=[])

    class Storage:
        async def remove_object(self, key):
            state.calls.append(("object", key))
            if "object" in state.fail:
                raise OSError("storage unavailable")
            state.objects.discard(key)

    class Vector:
        def delete_document(self, collection, document_id):
            state.calls.append(("vector", collection, document_id))
            if collection in state.fail:
                raise OSError("vector unavailable")
            state.vectors.discard((collection, document_id))

    class Graph:
        def delete_document(self, dataset_id, document_id):
            assert dataset_id == "dataset"
            state.calls.append(("graph", document_id))
            if "graph" in state.fail:
                raise OSError("graph unavailable")
            state.graph.discard(document_id)

    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", lambda *a, **kw: engine)
    monkeypatch.setattr("rag_modules.object_storage.factory.get_object_storage", lambda: Storage())
    monkeypatch.setattr("rag_modules.vector_stores.factory.get_vector_store", lambda *a: Vector())
    monkeypatch.setattr("rag_modules.graph.factory.get_graph_store", lambda **kwargs: Graph())
    yield factory, state
    await engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_removes_all_index_versions_and_is_idempotent(cleanup_env):
    factory, state = cleanup_env
    await tasks._cleanup_document("dataset", "doc")
    assert state.objects == {"other-source"}
    assert state.vectors == {(i, "other") for i in ["active", "old", "building"]}
    assert state.graph == {"other"}
    async with factory() as db:
        assert (await db.get(DocumentRecord, "doc")).cleanup_completed_at is not None
        assert (await db.get(DocumentSegmentRecord, "s1")).deleted_at is not None
        assert (await db.get(DocumentSegmentRecord, "s2")).deleted_at is not None
        assert (await db.get(DocumentSegmentRecord, "s3")).deleted_at is None
    calls = list(state.calls)
    await tasks._cleanup_document("dataset", "doc")
    assert state.calls == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["object", "active", "graph"])
async def test_one_failed_store_does_not_block_others_and_next_attempt_recovers(cleanup_env, failure):
    factory, state = cleanup_env
    state.fail.add(failure)
    await tasks._cleanup_document("dataset", "doc")
    assert ("vector", "building", "doc") in state.calls
    assert ("graph", "doc") in state.calls
    async with factory() as db:
        assert (await db.get(DocumentSegmentRecord, "s1")).deleted_at is not None
        assert getattr(await db.get(DocumentRecord, "doc"), "cleanup_completed_at", None) is None
    state.fail.clear()
    await tasks._cleanup_document("dataset", "doc")
    assert state.objects == {"other-source"}
    assert state.graph == {"other"}
    assert state.vectors == {(i, "other") for i in ["active", "old", "building"]}
    async with factory() as db:
        assert (await db.get(DocumentRecord, "doc")).cleanup_completed_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("dataset, document", [("dataset", "other"), ("wrong", "doc"), ("dataset", "missing")])
async def test_cleanup_never_touches_live_missing_or_wrong_dataset(cleanup_env, dataset, document):
    _, state = cleanup_env
    await tasks._cleanup_document(dataset, document)
    assert state.calls == []


@pytest.mark.asyncio
async def test_cleanup_waits_until_running_index_writer_finishes(cleanup_env):
    factory, state = cleanup_env
    async with factory() as db:
        db.add(IndexingJobDocumentRecord(id="running", job_id="job", document_id="doc", status="running", lease_expires_at=utcnow() + timedelta(minutes=5)))
        await db.commit()
    await tasks._cleanup_document("dataset", "doc")
    async with factory() as db:
        assert getattr(await db.get(DocumentRecord, "doc"), "cleanup_completed_at", None) is None
        row = await db.get(IndexingJobDocumentRecord, "running")
        row.status = "cancelled"
        await db.commit()
    # Simulate a final write from the operation that was in flight at deletion.
    state.vectors.add(("active", "doc"))
    await tasks._cleanup_document("dataset", "doc")
    assert ("active", "doc") not in state.vectors
    async with factory() as db:
        assert (await db.get(DocumentRecord, "doc")).cleanup_completed_at is not None


@pytest.mark.asyncio
async def test_beat_rediscovers_pending_cleanup_after_publish_or_store_failure(cleanup_env, monkeypatch):
    factory, _ = cleanup_env
    delivered = []
    def dispatch(**kwargs):
        delivered.append(kwargs)
        if len(delivered) == 1:
            raise OSError("broker unavailable")
    monkeypatch.setattr("rag_modules.tasks.publisher.get_task_publisher", lambda: SimpleNamespace(dispatch_document_cleanup=dispatch))
    await tasks._dispatch_pending_cleanup()
    await tasks._dispatch_pending_cleanup()
    assert delivered == [{"dataset_id": "dataset", "document_id": "doc"}] * 2
    await tasks._cleanup_document("dataset", "doc")
    await tasks._dispatch_pending_cleanup()
    assert len(delivered) == 2


@pytest.mark.asyncio
async def test_indexing_detects_document_deletion_despite_stale_identity_map(cleanup_env):
    from rag_modules.repositories.indexing_repository import IndexingRepository
    factory, _ = cleanup_env
    async with factory() as stale, factory() as deleting:
        document = await stale.get(DocumentRecord, "other")
        assert document.deleted_at is None
        stale.add(IndexingJobDocumentRecord(id="running", job_id="job", document_id="other", status="running", worker_id="w"))
        await stale.commit()
        fresh = await deleting.get(DocumentRecord, "other")
        fresh.deleted_at = utcnow()
        await deleting.commit()
        assert await IndexingRepository(stale).cancellation_requested("running", worker_id="w") is True


@pytest.mark.asyncio
async def test_indexing_cannot_claim_a_deleted_document(cleanup_env):
    from rag_modules.repositories.indexing_repository import IndexingRepository
    factory, _ = cleanup_env
    async with factory() as db:
        row = IndexingJobDocumentRecord(id="pending", job_id="job", document_id="doc", status="pending", available_at=utcnow())
        db.add(row)
        await db.commit()
        assert await IndexingRepository(db).claim_job_document("pending", "w") is None


@pytest.mark.asyncio
async def test_late_writer_after_lease_recovery_reopens_completed_cleanup(cleanup_env, monkeypatch):
    from rag_modules.repositories.indexing_repository import IndexingRepository
    from rag_modules.tasks import runtime
    factory, state = cleanup_env
    async with factory() as db:
        db.add(IndexingJobDocumentRecord(id="expired", job_id="job", document_id="doc", status="running", worker_id="old", lease_expires_at=utcnow() - timedelta(seconds=1)))
        await db.commit()

    async def finish_late_writer(_identifier):
        async with factory() as db:
            assert await IndexingRepository(db).recover_expired_documents() == ["expired"]
        await tasks._cleanup_document("dataset", "doc")
        async with factory() as db:
            assert (await db.get(DocumentRecord, "doc")).cleanup_completed_at is not None
        # The recovered worker's remote operation finishes after cleanup.
        state.vectors.add(("active", "doc"))
        state.graph.add("doc")

    async def build(*args, **kwargs):
        return SimpleNamespace(run=finish_late_writer)
    monkeypatch.setattr("rag_modules.tasks.indexing_tasks.build_task_runner", build)
    monkeypatch.setattr(runtime, "create_async_engine", lambda *args, **kwargs: factory.kw["bind"])
    await runtime._run_indexing_task("expired")
    delivered = []
    monkeypatch.setattr("rag_modules.tasks.publisher.get_task_publisher", lambda: SimpleNamespace(dispatch_document_cleanup=lambda **kwargs: delivered.append(kwargs)))
    await tasks._dispatch_pending_cleanup()
    assert delivered == [{"dataset_id": "dataset", "document_id": "doc"}]
    await tasks._cleanup_document("dataset", "doc")
    assert ("active", "doc") not in state.vectors
    assert "doc" not in state.graph


@pytest.mark.asyncio
async def test_compensation_database_failure_is_retryable_and_redelivery_reopens_cleanup(cleanup_env, monkeypatch):
    from contextlib import asynccontextmanager
    from rag_modules.tasks import runtime
    factory, _ = cleanup_env
    await tasks._cleanup_document("dataset", "doc")
    async with factory() as db:
        db.add(IndexingJobDocumentRecord(id="done", job_id="job", document_id="doc", status="retry_wait"))
        await db.commit()
    calls = 0
    @asynccontextmanager
    async def flaky_factory():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConnectionError("database temporarily unavailable")
        async with factory() as session:
            yield session
    async def no_runner(*args, **kwargs):
        return None
    monkeypatch.setattr(runtime, "create_async_engine", lambda *a, **kw: factory.kw["bind"])
    monkeypatch.setattr(runtime, "async_sessionmaker", lambda *a, **kw: flaky_factory)
    monkeypatch.setattr("rag_modules.tasks.indexing_tasks.build_task_runner", no_runner)
    with pytest.raises(RuntimeError, match="cleanup compensation must be retried"):
        await runtime._run_indexing_task("done")
    async with factory() as db:
        assert (await db.get(DocumentRecord, "doc")).cleanup_completed_at is not None
    await runtime._run_indexing_task("done")
    async with factory() as db:
        assert (await db.get(DocumentRecord, "doc")).cleanup_completed_at is None


@pytest.mark.asyncio
async def test_failed_cleanup_dispatches_do_not_starve_newer_deletions(cleanup_env, monkeypatch):
    factory, _ = cleanup_env
    async with factory() as db:
        db.add_all([DocumentRecord(id=f"pending-{i}", dataset_id="dataset", name="file.txt", data_source_type="upload", created_from="api", created_by="u", indexing_status="failed", deleted_at=utcnow()) for i in range(120)])
        await db.commit()
    delivered = set()
    def unavailable(**kwargs):
        delivered.add(kwargs["document_id"])
        raise OSError("broker unavailable")
    monkeypatch.setattr("rag_modules.tasks.publisher.get_task_publisher", lambda: SimpleNamespace(dispatch_document_cleanup=unavailable))
    await tasks._dispatch_pending_cleanup()
    await tasks._dispatch_pending_cleanup()
    assert delivered == {"doc"} | {f"pending-{i}" for i in range(120)}
