from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rag_modules.db.base import Base
from rag_modules.db.models import DatasetIndexRecord, DocumentRecord, DocumentSegmentRecord
from rag_modules.repositories.document_repository import DocumentRepository


@pytest_asyncio.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'documents.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # SQLite cannot enforce the PostgreSQL partial unique index used by the model.
        active_index = next(
            index for index in DatasetIndexRecord.__table__.indexes
            if index.name == "uq_dataset_indexes_one_active"
        )
        await connection.run_sync(active_index.drop)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


def make_document(document_id="doc-1", dataset_id="dataset-1"):
    return DocumentRecord(
        id=document_id,
        dataset_id=dataset_id,
        position=1,
        data_source_type="upload_file",
        data_source_info={"object_key": "datasets/dataset-1/documents/doc-1/source.txt"},
        name="guide.txt",
        created_from="api",
        created_by="user-1",
        indexing_status="completed",
    )


@pytest.mark.asyncio
async def test_document_repository_mutations_are_scoped_and_idempotent(session):
    document = make_document()
    session.add(document)
    await session.commit()
    repository = DocumentRepository(session)

    renamed = await repository.update_name("dataset-1", "doc-1", "renamed.txt", "user-2")
    assert renamed.name == "renamed.txt"
    assert renamed.data_source_info["object_key"].endswith("source.txt")
    assert renamed.data_source_info["original_filename"] == "renamed.txt"

    await repository.set_enabled("dataset-1", "doc-1", False, "user-2")
    await repository.set_enabled("dataset-1", "doc-1", False, "user-2")
    await repository.set_archived("dataset-1", "doc-1", True, "user-2")
    await repository.set_archived("dataset-1", "doc-1", True, "user-2")
    deleted = await repository.soft_delete("dataset-1", "doc-1")
    assert deleted.deleted_at is not None
    assert await repository.get_active_document("dataset-1", "doc-1") is None


@pytest.mark.asyncio
async def test_document_repository_lists_only_active_segments(session):
    document = make_document()
    session.add(document)
    session.add_all([
        DocumentSegmentRecord(
            id="segment-1", document_id="doc-1", dataset_id="dataset-1", position=1,
            content="one", status="completed", index_type="general", embedding_status="completed",
        ),
        DocumentSegmentRecord(
            id="segment-2", document_id="doc-1", dataset_id="dataset-1", position=2,
            content="old", status="completed", index_type="general", embedding_status="completed",
            deleted_at=datetime.now(timezone.utc),
        ),
    ])
    await session.commit()

    records = await DocumentRepository(session).list_segments("dataset-1", "doc-1")
    assert [record.id for record in records] == ["segment-1"]
