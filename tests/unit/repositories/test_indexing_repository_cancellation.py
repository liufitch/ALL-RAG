import pytest
from uuid import uuid4
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from rag_modules.db.base import Base
from rag_modules.db.models import (
    DatasetRecord,
    DocumentRecord,
    DocumentRevisionRecord,
    IndexingJobRecord,
    IndexingJobDocumentRecord,
)
from rag_modules.repositories.indexing_repository import IndexingRepository


@pytest.mark.asyncio
async def test_cancel_job_document_with_revision_succeeds_without_error_reference():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    dataset_id = uuid4().hex
    doc_id = uuid4().hex
    job_id = uuid4().hex
    job_doc_id = uuid4().hex
    revision_id = uuid4().hex
    worker_id = "test-worker"

    async with session_factory() as session:
        dataset = DatasetRecord(id=dataset_id, name="Test DS", provider="test", permission="only_me", indexing_technique="economy", created_by="u1")
        doc = DocumentRecord(id=doc_id, dataset_id=dataset_id, name="doc.txt", data_source_type="upload", created_from="console", created_by="u1", indexing_status="running")
        job = IndexingJobRecord(
            id=job_id,
            dataset_id=dataset_id,
            job_type="document_reindex",
            scope="single_document",
            status="running",
            indexing_technique="economy",
            segmentation_mode="general",
            process_rule={},
            retrieval_config={},
            total_documents=1,
            created_by="u1",
        )
        job_doc = IndexingJobDocumentRecord(
            id=job_doc_id,
            job_id=job_id,
            document_id=doc_id,
            status="running",
            worker_id=worker_id,
        )
        rev = DocumentRevisionRecord(
            id=revision_id,
            dataset_id=dataset_id,
            document_id=doc_id,
            version=1,
            status="indexing",
            indexing_job_id=job_id,
            segments={},
            created_by="u1",
        )
        session.add_all([dataset, doc, job, job_doc, rev])
        await session.commit()

        repo = IndexingRepository(session)
        await repo.cancel_job_document(job_doc_id, worker_id=worker_id)

        await session.refresh(job_doc)
        await session.refresh(doc)
        await session.refresh(rev)

        assert job_doc.status == "cancelled"
        assert doc.indexing_status == "cancelled"
        assert rev.status == "cancelled"
        assert rev.error is None


@pytest.mark.asyncio
async def test_heartbeat_refreshes_job_summary_and_graph_stage_progress():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    dataset_id = uuid4().hex
    doc_id = uuid4().hex
    job_id = uuid4().hex
    job_doc_id = uuid4().hex

    async with session_factory() as session:
        session.add_all([
            DatasetRecord(id=dataset_id, name="Test DS", provider="test", permission="only_me", indexing_technique="economy", created_by="u1"),
            DocumentRecord(id=doc_id, dataset_id=dataset_id, name="doc.txt", data_source_type="upload", created_from="console", created_by="u1", indexing_status="waiting"),
            IndexingJobRecord(id=job_id, dataset_id=dataset_id, job_type="initial_index", scope="selected_documents", status="pending", indexing_technique="economy", segmentation_mode="general", process_rule={}, retrieval_config={}, created_by="u1"),
            IndexingJobDocumentRecord(id=job_doc_id, job_id=job_id, document_id=doc_id, status="running", worker_id="worker-1"),
        ])
        await session.commit()

        await IndexingRepository(session).heartbeat(
            job_doc_id, worker_id="worker-1", stage="graph-extract", progress=94,
            processed_segments=1, total_segments=2,
        )
        job = await session.get(IndexingJobRecord, job_id)
        job_doc = await session.get(IndexingJobDocumentRecord, job_doc_id)

        assert job.status == "running"
        assert job.progress == 94
        assert job.current_stage == "graph-extract"
        assert job_doc.total_segments == 2
        assert job_doc.processed_segments == 1

    await engine.dispose()
