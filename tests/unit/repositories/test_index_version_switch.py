from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rag_modules.db.base import Base
from rag_modules.db.models import (
    DatasetIndexRecord,
    DatasetRecord,
    DocumentSegmentRecord,
    IndexingJobDocumentRecord,
    IndexingJobRecord,
)
from rag_modules.repositories.indexing_repository import IndexingRepository


@pytest_asyncio.fixture
async def indexing_database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'index-switch.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(
            lambda sync_connection: next(
                index
                for index in DatasetIndexRecord.__table__.indexes
                if index.name == "uq_dataset_indexes_one_active"
            ).drop(sync_connection)
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed_build(session, *, document_statuses: tuple[str, ...]):
    dataset_id = "dataset-1"
    old_job_id = uuid4().hex
    old_index_id = uuid4().hex
    job_id = uuid4().hex
    target_index_id = uuid4().hex
    session.add(
        DatasetRecord(
            id=dataset_id,
            name="Dataset",
            provider="vendor",
            permission="private",
            indexing_technique="high_quality",
            created_by="user-1",
        )
    )
    session.add(
        IndexingJobRecord(
            id=old_job_id,
            dataset_id=dataset_id,
            job_type="initial_index",
            scope="selected_documents",
            status="completed",
            indexing_technique="high_quality",
            segmentation_mode="general",
            process_rule={"segmentation": {"mode": "general"}},
            retrieval_config={},
            created_by="user-1",
        )
    )
    session.add(
        DatasetIndexRecord(
            id=old_index_id,
            dataset_id=dataset_id,
            created_by_job_id=old_job_id,
            index_type="high_quality",
            status="active",
            process_rule={"segmentation": {"mode": "general"}},
            retrieval_config={},
            config_hash="old",
            activated_at=None,
        )
    )
    session.add(
        IndexingJobRecord(
            id=job_id,
            dataset_id=dataset_id,
            target_index_id=target_index_id,
            job_type="reindex_dataset",
            scope="entire_dataset",
            status="running",
            indexing_technique="high_quality",
            segmentation_mode="general",
            process_rule={"segmentation": {"mode": "general"}},
            retrieval_config={},
            created_by="user-1",
        )
    )
    session.add(
        DatasetIndexRecord(
            id=target_index_id,
            dataset_id=dataset_id,
            created_by_job_id=job_id,
            index_type="high_quality",
            status="building",
            process_rule={"segmentation": {"mode": "general"}},
            retrieval_config={},
            config_hash="new",
        )
    )
    for position, status in enumerate(document_statuses):
        document_id = f"doc-{position + 1}"
        job_document_id = uuid4().hex
        session.add(
            IndexingJobDocumentRecord(
                id=job_document_id,
                job_id=job_id,
                document_id=document_id,
                status=status,
                progress=100 if status == "completed" else 0,
            )
        )
        if status == "completed":
            session.add(
                DocumentSegmentRecord(
                    id=uuid4().hex,
                    document_id=document_id,
                    dataset_id=dataset_id,
                    dataset_index_id=target_index_id,
                    indexing_job_id=job_id,
                    position=0,
                    content=f"new content {position}",
                    status="indexing",
                    index_type="general",
                    embedding_status="completed",
                )
            )
    await session.commit()
    return old_index_id, target_index_id, job_id


@pytest.mark.asyncio
async def test_finalization_waits_until_every_document_is_terminal(indexing_database):
    old_id, target_id, job_id = await _seed_build(indexing_database, document_statuses=("completed", "running"))

    finalized = await IndexingRepository(indexing_database).finalize_indexing_job(job_id)

    assert finalized is False
    assert (await indexing_database.get(DatasetIndexRecord, old_id)).status == "active"
    assert (await indexing_database.get(DatasetIndexRecord, target_id)).status == "building"


@pytest.mark.asyncio
async def test_failed_build_marks_target_failed_and_keeps_old_version_active(indexing_database):
    old_id, target_id, job_id = await _seed_build(indexing_database, document_statuses=("completed", "failed"))

    finalized = await IndexingRepository(indexing_database).finalize_indexing_job(job_id)

    assert finalized is True
    assert (await indexing_database.get(DatasetIndexRecord, old_id)).status == "active"
    assert (await indexing_database.get(DatasetIndexRecord, target_id)).status == "failed"


@pytest.mark.asyncio
async def test_invalid_staged_segments_fail_build_without_touching_old_version(indexing_database):
    old_id, target_id, job_id = await _seed_build(indexing_database, document_statuses=("completed",))
    segment = await indexing_database.scalar(
        select(DocumentSegmentRecord).where(DocumentSegmentRecord.dataset_index_id == target_id)
    )
    segment.embedding_status = "waiting"
    await indexing_database.commit()

    assert await IndexingRepository(indexing_database).finalize_indexing_job(job_id) is True

    assert (await indexing_database.get(DatasetIndexRecord, old_id)).status == "active"
    assert (await indexing_database.get(DatasetIndexRecord, target_id)).status == "failed"


@pytest.mark.asyncio
async def test_successful_build_switches_once_and_activates_staged_segments(indexing_database):
    old_id, target_id, job_id = await _seed_build(indexing_database, document_statuses=("completed", "completed"))

    repository = IndexingRepository(indexing_database)
    assert await repository.finalize_indexing_job(job_id) is True
    assert await repository.finalize_indexing_job(job_id) is True

    assert (await indexing_database.get(DatasetIndexRecord, old_id)).status == "retired"
    assert (await indexing_database.get(DatasetIndexRecord, target_id)).status == "active"
    segments = list((await indexing_database.execute(select(DocumentSegmentRecord))).scalars())
    assert segments and all(segment.status == "completed" for segment in segments)
    assert await indexing_database.scalar(
        select(DatasetIndexRecord.id).where(
            DatasetIndexRecord.dataset_id == "dataset-1",
            DatasetIndexRecord.status == "active",
        )
    ) == target_id


@pytest.mark.asyncio
async def test_incremental_job_activates_staged_segments_without_switching_index(indexing_database):
    old_id, target_id, job_id = await _seed_build(indexing_database, document_statuses=("completed",))
    job = await indexing_database.get(IndexingJobRecord, job_id)
    target = await indexing_database.get(DatasetIndexRecord, target_id)
    job.job_type = "add_documents"
    job.scope = "selected_documents"
    target.status = "active"
    old_index = await indexing_database.get(DatasetIndexRecord, old_id)
    old_index.status = "retired"
    indexing_database.add(
        DocumentSegmentRecord(
            id=uuid4().hex,
            document_id="doc-1",
            dataset_id="dataset-1",
            dataset_index_id=target_id,
            indexing_job_id="old-job",
            position=0,
            content="old content",
            status="completed",
            index_type="general",
            embedding_status="completed",
        )
    )
    await indexing_database.commit()

    assert await IndexingRepository(indexing_database).finalize_indexing_job(job_id) is True

    segments = list((await indexing_database.execute(select(DocumentSegmentRecord))).scalars())
    assert len(segments) == 2
    assert next(segment for segment in segments if segment.content == "new content 0").status == "completed"
    assert next(segment for segment in segments if segment.content == "old content").deleted_at is not None
    assert (await indexing_database.get(DatasetIndexRecord, target_id)).status == "active"
    assert (await indexing_database.get(DatasetIndexRecord, old_id)).status == "retired"
