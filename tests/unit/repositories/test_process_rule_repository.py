import pytest
import pytest_asyncio
from sqlalchemy import CheckConstraint, MetaData, event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rag_modules.api.dto.process_rule import ProcessRuleConfirmationRequest
from rag_modules.db.base import Base
from rag_modules.db.models import (
    DatasetIndexRecord, DatasetRecord, DocumentRecord,
    IndexingJobDocumentRecord, IndexingJobRecord,
    DocumentRevisionRecord,
)
from rag_modules.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag_modules.repositories.process_rule_repository import ProcessRuleRepository
from rag_modules.services.process_rule_service import ProcessRuleService


@pytest_asyncio.fixture
async def constrained_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'process-rule.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    # Reproduce the approved migration's immediate checks as well as FKs;
    # metadata.create_all alone omits these PostgreSQL constraints.
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    for index in metadata.tables["dataset_indexes"].indexes:
        if index.name == "uq_dataset_indexes_one_active":
            index.dialect_options["sqlite"]["where"] = text("status = 'active' AND deleted_at IS NULL")
    metadata.tables["indexing_jobs"].append_constraint(CheckConstraint(
        "job_type IN ('initial_index', 'add_documents', 'reindex_documents', 'reindex_dataset')",
        name="ck_indexing_jobs_job_type",
    ))
    metadata.tables["indexing_jobs"].append_constraint(CheckConstraint(
        "scope IN ('selected_documents', 'entire_dataset')", name="ck_indexing_jobs_scope",
    ))
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(DatasetRecord(
            id="dataset-1", name="Test", provider="local", permission="only_me",
            indexing_technique="economy", created_by="tester",
        ))
        await session.commit()
        session.add(DocumentRecord(
            id="doc-1", dataset_id="dataset-1", position=0, name="scan.png",
            data_source_type="upload_file", created_from="api", created_by="tester",
            indexing_status="waiting",
        ))
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["confirm", "rebuild"])
async def test_index_creation_respects_job_index_foreign_key_cycle(constrained_session, operation):
    """真实外键启用时，先保存任务和索引，再绑定双向引用。"""
    session = constrained_session
    if operation == "confirm":
        result = await ProcessRuleService(ProcessRuleRepository(session)).confirm(
            "dataset-1",
            ProcessRuleConfirmationRequest(
                document_ids=["doc-1"], indexing_technique="economy",
                segmentation={"mode": "general", "max_chunk_length": 500, "overlap": 50},
            ),
            "tester",
        )
        job = result.job
        assert (await session.get(DocumentRecord, "doc-1")).dataset_process_rule_id == result.rule.id
    else:
        job, _ = await KnowledgeBaseRepository(session).create_rebuild_job("dataset-1", "tester")

    job_id = job.id
    session.expire_all()
    saved_job = await session.get(IndexingJobRecord, job_id)
    index = await session.get(DatasetIndexRecord, saved_job.target_index_id)
    assert index.created_by_job_id == saved_job.id
    assert index.status == "building"
    row = await session.scalar(select(IndexingJobDocumentRecord).where(IndexingJobDocumentRecord.job_id == saved_job.id))
    assert row.document_id == "doc-1"


@pytest.mark.asyncio
async def test_segment_revision_persists_job_before_revision_foreign_key(constrained_session):
    from rag_modules.api.dto.document import DocumentSegmentUpdate
    from rag_modules.services.document_revision_service import DocumentRevisionService

    session = constrained_session
    created = await ProcessRuleService(ProcessRuleRepository(session)).confirm(
        "dataset-1", ProcessRuleConfirmationRequest(
            document_ids=["doc-1"], indexing_technique="economy",
            segmentation={"mode": "general", "max_chunk_length": 500, "overlap": 50},
        ), "tester",
    )
    index = await session.get(DatasetIndexRecord, created.job.target_index_id)
    index.status = "active"
    created.job.status = "completed"
    await session.commit()

    result = await DocumentRevisionService(session).create_revision_and_job(
        "dataset-1", "doc-1", DocumentSegmentUpdate(
            base_version=0,
            segments=[{"id": "segment-1", "position": 0, "content": "Corrected OCR", "index_type": "general"}],
        ), "tester",
    )

    saved_revision = await session.get(DocumentRevisionRecord, result.revision.id)
    saved_job = await session.get(IndexingJobRecord, result.job.id)
    assert saved_revision.indexing_job_id == saved_job.id
    assert saved_job.revision_id == saved_revision.id
    assert saved_revision.segments["items"][0]["content"] == "Corrected OCR"


@pytest.mark.asyncio
async def test_confirmed_configuration_is_used_by_settings_and_rebuild(constrained_session):
    from rag_modules.services.knowledge_base_service import KnowledgeBaseService

    session = constrained_session
    dataset = await session.get(DatasetRecord, "dataset-1")
    dataset.indexing_technique = "high_quality"
    dataset.embedding_model = "bge-m3"
    dataset.partial_user_config = {"custom": "preserved"}
    await session.commit()

    await ProcessRuleService(ProcessRuleRepository(session)).confirm(
        "dataset-1", ProcessRuleConfirmationRequest(
            document_ids=["doc-1"], indexing_technique="economy",
            segmentation={"mode": "general", "max_chunk_length": 700, "overlap": 70},
        ), "tester",
    )
    settings = await KnowledgeBaseService(KnowledgeBaseRepository(session)).get_settings("dataset-1")
    assert settings.indexing["technique"] == "economy"
    assert settings.indexing["embedding_model"] is None
    assert settings.indexing["process_rule"]["max_chunk_length"] == 700
    assert dataset.partial_user_config["custom"] == "preserved"

    job, _ = await KnowledgeBaseRepository(session).create_rebuild_job("dataset-1", "tester")
    assert job.indexing_technique == "economy"
    assert job.embedding_model is None
    assert job.process_rule["segmentation"]["overlap"] == 70


@pytest.mark.asyncio
async def test_single_document_reindex_without_active_index_respects_foreign_keys(constrained_session):
    from rag_modules.repositories.indexing_repository import IndexingRepository

    session = constrained_session
    row_id = await IndexingRepository(session).ensure_compatibility_job_document("dataset-1", "doc-1")
    row = await session.get(IndexingJobDocumentRecord, row_id)
    job = await session.get(IndexingJobRecord, row.job_id)
    index = await session.get(DatasetIndexRecord, job.target_index_id)
    assert index.created_by_job_id == job.id
    assert job.job_type == "initial_index"
