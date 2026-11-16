"""Manual edits must survive rebuild, retry and portable archive round trips."""
from contextlib import asynccontextmanager
from io import BytesIO
import json
from pathlib import Path
import zipfile

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rag_modules.common import utcnow
from rag_modules.db.base import Base
from rag_modules.db.models import DatasetRecord, DocumentRecord, DocumentRevisionRecord, IndexingJobDocumentRecord
from rag_modules.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag_modules.repositories.indexing_repository import IndexingRepository
from rag_modules.services.knowledge_base_archive_service import KnowledgeBaseArchiveService, ArchiveValidationError
from rag_modules.tasks.indexing_tasks import build_task_runner


def segment(identifier, content, **kwargs):
    return dict(id=identifier, position=0, content=content, question=None, answer=None, keywords=["manual"], parent_id=None, index_type="general", source_metadata={"page": 2}, **kwargs)


class MemoryStorage:
    def __init__(self):
        self.objects = {"original": b"Original source", "plain": b"Plain source"}
    @asynccontextmanager
    async def get_stream(self, key):
        yield BytesIO(self.objects[key])
    async def put_stream(self, key, stream, length, content_type):
        self.objects[key] = stream.read()
    async def remove_object(self, key):
        self.objects.pop(key, None)


@pytest_asyncio.fixture
async def revision_env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DROP INDEX uq_dataset_indexes_one_active"))
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add(DatasetRecord(id="source", name="Knowledge", provider="vendor", permission="only_me", indexing_technique="economy", created_by="u"))
        db.add_all([DocumentRecord(id=doc, dataset_id="source", name=f"{doc}.txt", position=i, data_source_type="upload_file", data_source_info={"object_key": key}, created_from="api", created_by="u", indexing_status="completed", enabled=True, archived=False) for i, (doc, key) in enumerate([("edited", "original"), ("plain", "plain")])])
        db.add_all([DocumentRevisionRecord(id=f"revision-{version}", dataset_id="source", document_id="edited", version=version, segments={"items": [segment(f"s{version}", content)]}, status="completed", created_by="editor", created_at=utcnow()) for version, content in [(1, "Earlier manual edit"), (2, "Latest manual edit")]])
        await db.commit()
        yield db, MemoryStorage()
    await engine.dispose()


@pytest.mark.asyncio
async def test_rebuild_pins_each_document_revision_and_retry_does_not_read_newer_edit(revision_env):
    db, _ = revision_env
    job, rows = await KnowledgeBaseRepository(db).create_rebuild_job("source", "actor")
    edited = next(row for row in rows if row.document_id == "edited")
    plain = next(row for row in rows if row.document_id == "plain")
    assert getattr(edited, "revision_id", None) == "revision-2"
    assert plain.revision_id is None
    db.add(DocumentRevisionRecord(id="revision-3", dataset_id="source", document_id="edited", version=3, segments={"items": [segment("s3", "Later edit after enqueue")]}, status="completed", created_by="editor"))
    await db.commit()
    runner = await build_task_runner(db, job_document_id=edited.id)
    try:
        command = runner.command_factory(edited)
        assert [item.content for item in command.revision_segments] == ["Latest manual edit"]
        assert command.revision_segments[0].source_metadata["revision_id"] == "revision-2"
        edited.status = "failed"
        job.status = "failed"
        await db.commit()
        ok, _, _ = await IndexingRepository(db).retry_job("source", job.id)
        assert ok
        assert edited.revision_id == "revision-2"
    finally:
        await runner.engine.aclose()
    runner = await build_task_runner(db, job_document_id=plain.id)
    try:
        assert runner.command_factory(plain).revision_segments is None
    finally:
        await runner.engine.aclose()


@pytest.mark.asyncio
async def test_export_import_rebuild_preserves_revision_history_and_original_files(revision_env, tmp_path):
    db, storage = revision_env
    service = KnowledgeBaseArchiveService(KnowledgeBaseRepository(db), storage, temp_dir=tmp_path)
    path = await service.export_dataset("source")
    with zipfile.ZipFile(path) as archive:
        assert "revisions.json" in archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format_version"] == 2
        assert any(entry["path"] == "revisions.json" for entry in manifest["files"])
        assert len(json.loads(archive.read("revisions.json"))) == 2
    with open(path, "rb") as source:
        result = await service.import_dataset(source, actor_id="importer")
    dataset_id = result["dataset_id"]
    revisions = (await db.scalars(select(DocumentRevisionRecord).where(DocumentRevisionRecord.dataset_id == dataset_id).order_by(DocumentRevisionRecord.version))).all()
    assert [revision.version for revision in revisions] == [1, 2]
    assert [revision.segments["items"][0]["content"] for revision in revisions] == ["Earlier manual edit", "Latest manual edit"]
    assert all(revision.id not in {"revision-1", "revision-2"} and revision.document_id != "edited" and revision.indexing_job_id is None for revision in revisions)
    documents = await KnowledgeBaseRepository(db).list_all_documents(dataset_id)
    assert {storage.objects[doc.data_source_info["object_key"]] for doc in documents} == {b"Original source", b"Plain source"}
    job, rows = await KnowledgeBaseRepository(db).create_rebuild_job(dataset_id, "importer")
    row = next(row for row in rows if row.document_id == revisions[-1].document_id)
    runner = await build_task_runner(db, job_document_id=row.id)
    try:
        assert [item.content for item in runner.command_factory(row).revision_segments] == ["Latest manual edit"]
    finally:
        await runner.engine.aclose()


@pytest.mark.asyncio
async def test_tampered_revision_archive_is_rejected_before_uploading_objects(revision_env, tmp_path):
    db, storage = revision_env
    service = KnowledgeBaseArchiveService(KnowledgeBaseRepository(db), storage, temp_dir=tmp_path)
    path = await service.export_dataset("source")
    altered = BytesIO()
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(altered, "w") as output:
        for name in source.namelist():
            output.writestr(name, b'[]' if name == "revisions.json" else source.read(name))
    before = dict(storage.objects)
    altered.seek(0)
    with pytest.raises(ArchiveValidationError):
        await service.import_dataset(altered)
    assert storage.objects == before


@pytest.mark.asyncio
async def test_import_then_execute_full_rebuild_retrieves_manual_edits(revision_env, tmp_path, monkeypatch):
    from rag_modules.tasks import indexing_tasks
    from rag_modules.retrieval import RetrievalService
    from rag_modules.db.models import DocumentSegmentRecord
    db, storage = revision_env
    service = KnowledgeBaseArchiveService(KnowledgeBaseRepository(db), storage, temp_dir=tmp_path)
    path = await service.export_dataset("source")
    with open(path, "rb") as source:
        imported = await service.import_dataset(source)
    dataset_id = imported["dataset_id"]
    job, rows = await KnowledgeBaseRepository(db).create_rebuild_job(dataset_id, "importer")
    monkeypatch.setattr(indexing_tasks, "get_object_storage", lambda: storage)
    monkeypatch.setattr(indexing_tasks, "make_graph_indexer", lambda: None)
    for row in rows:
        runner = await build_task_runner(db, job_document_id=row.id)
        await runner.run(row.id)
    assert job.status == "completed"
    segments = (await db.scalars(select(DocumentSegmentRecord).where(DocumentSegmentRecord.dataset_id == dataset_id, DocumentSegmentRecord.deleted_at.is_(None)))).all()
    assert {item.content for item in segments} == {"Latest manual edit", "Plain source"}
    hits = await RetrievalService(db).retrieve(dataset_id, "manual", mode="full_text", score_threshold=0)
    assert [hit.content for hit in hits.items] == ["Latest manual edit"]


@pytest.mark.asyncio
async def test_parent_child_snapshots_keep_keywords_locations_and_parent_links(revision_env, tmp_path):
    db, storage = revision_env
    revision = await db.get(DocumentRevisionRecord, "revision-2")
    parent = segment("parent", "Full parent context")
    parent["index_type"] = "parent"
    child = segment("child", "Corrected child")
    child.update(parent_id="parent", index_type="child", position=1, question="Edited question", answer="Edited answer")
    revision.segments = {"items": [parent, child]}
    await db.commit()
    service = KnowledgeBaseArchiveService(KnowledgeBaseRepository(db), storage, temp_dir=tmp_path)
    path = await service.export_dataset("source")
    with open(path, "rb") as source:
        result = await service.import_dataset(source)
    imported = await db.scalar(select(DocumentRevisionRecord).where(DocumentRevisionRecord.dataset_id == result["dataset_id"], DocumentRevisionRecord.version == 2))
    items = imported.segments["items"]
    assert items[0]["id"] != "parent" and items[1]["id"] != "child"
    assert items[1]["parent_id"] == items[0]["id"]
    assert items[1]["question"] == "Edited question" and items[1]["answer"] == "Edited answer"
    assert items[1]["keywords"] == ["manual"] and items[1]["source_metadata"] == {"page": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing", "unknown_document", "duplicate_version", "missing_parent", "empty", "negative_position", "overlong_id", "general_parent"])
async def test_invalid_revision_snapshots_rejected_before_any_object_write(revision_env, tmp_path, mutation):
    import hashlib
    db, storage = revision_env
    service = KnowledgeBaseArchiveService(KnowledgeBaseRepository(db), storage, temp_dir=tmp_path)
    path = await service.export_dataset("source")
    altered = BytesIO()
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(altered, "w") as output:
        revisions = json.loads(source.read("revisions.json"))
        if mutation == "unknown_document":
            revisions[0]["document_id"] = "another-dataset-document"
        elif mutation == "duplicate_version":
            revisions.append(revisions[0])
        elif mutation == "missing_parent":
            revisions[0]["segments"]["items"][0]["parent_id"] = "missing"
        elif mutation == "empty":
            revisions[0]["segments"]["items"] = []
        elif mutation == "negative_position":
            revisions[0]["segments"]["items"][0]["position"] = -1
        elif mutation == "overlong_id":
            revisions[0]["segments"]["items"][0]["id"] = "x" * 256
        elif mutation == "general_parent":
            parent = segment("p", "parent")
            parent["index_type"] = "parent"
            child = segment("s", "incorrect general child")
            child["parent_id"] = "p"
            revisions[0]["segments"]["items"] = [parent, child]
        data = json.dumps(revisions).encode()
        manifest = json.loads(source.read("manifest.json"))
        for entry in manifest["files"]:
            if entry["path"] == "revisions.json":
                entry.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        for name in source.namelist():
            if name == "revisions.json" and mutation == "missing":
                continue
            output.writestr(name, data if name == "revisions.json" else json.dumps(manifest).encode() if name == "manifest.json" else source.read(name))
    before = dict(storage.objects)
    altered.seek(0)
    with pytest.raises(ArchiveValidationError):
        await service.import_dataset(altered)
    assert storage.objects == before


@pytest.mark.asyncio
async def test_invalid_bound_revision_fails_job_without_falling_back_to_source(revision_env):
    db, _ = revision_env
    job, rows = await KnowledgeBaseRepository(db).create_rebuild_job("source", "actor")
    row = next(row for row in rows if row.document_id == "edited")
    row.revision_id = "missing"
    await db.commit()
    runner = await build_task_runner(db, job_document_id=row.id)
    await runner.run(row.id)
    assert row.status == "failed"
    assert row.error_code == "REVISION_SNAPSHOT_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_mode, configured_mode", [("general", "parent_child"), ("parent_child", "general")])
async def test_saved_snapshot_structure_overrides_changed_automatic_segmentation(revision_env, snapshot_mode, configured_mode):
    db, _ = revision_env
    dataset = await db.get(DatasetRecord, "source")
    dataset.indexing_technique = "high_quality"
    dataset.embedding_model = "bge-m3"
    dataset.partial_user_config = {"process_rule": {"mode": configured_mode}}
    if snapshot_mode == "parent_child":
        revision = await db.get(DocumentRevisionRecord, "revision-2")
        parent, child = segment("parent", "parent"), segment("child", "child")
        parent["index_type"] = "parent"
        child.update(index_type="child", parent_id="parent", position=1)
        revision.segments = {"items": [parent, child]}
    await db.commit()
    _, rows = await KnowledgeBaseRepository(db).create_rebuild_job("source", "actor")
    row = next(row for row in rows if row.document_id == "edited")
    runner = await build_task_runner(db, job_document_id=row.id)
    try:
        command = runner.command_factory(row)
        assert command.staging.segmentation_mode == snapshot_mode
        assert command.segmentation_config.mode == snapshot_mode
    finally:
        await runner.engine.aclose()


@pytest.mark.asyncio
async def test_economy_rebuild_keeps_all_parent_child_edits_as_independent_segments(revision_env, monkeypatch):
    from rag_modules.tasks import indexing_tasks
    from rag_modules.db.models import DocumentSegmentRecord
    db, storage = revision_env
    revision = await db.get(DocumentRevisionRecord, "revision-2")
    parent, child = segment("parent", "Corrected full context"), segment("child", "Corrected child detail")
    parent["index_type"] = "parent"
    child.update(index_type="child", parent_id="parent", position=1)
    revision.segments = {"items": [parent, child]}
    await db.commit()
    job, rows = await KnowledgeBaseRepository(db).create_rebuild_job("source", "actor")
    monkeypatch.setattr(indexing_tasks, "get_object_storage", lambda: storage)
    monkeypatch.setattr(indexing_tasks, "make_graph_indexer", lambda: None)
    for row in rows:
        runner = await build_task_runner(db, job_document_id=row.id)
        await runner.run(row.id)
    assert job.status == "completed"
    segments = (await db.scalars(select(DocumentSegmentRecord).where(DocumentSegmentRecord.document_id == "edited"))).all()
    assert {item.content for item in segments} == {"Corrected full context", "Corrected child detail"}
    assert all(item.index_type == "general" and item.parent_id is None for item in segments)
    assert revision.segments["items"][1]["parent_id"] == "parent"
