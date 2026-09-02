from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rag_modules.db.models import DatasetRecord, DocumentRecord
from rag_modules.repositories.document_repository import DocumentRepository


def _document(document_id, dataset_id, *, deleted_at=None):
    return DocumentRecord(
        id=document_id,
        dataset_id=dataset_id,
        position=1,
        data_source_type="upload_file",
        data_source_info={},
        name=f"{document_id}.txt",
        created_from="api",
        created_by="u",
        indexing_status="waiting",
        deleted_at=deleted_at,
    )


@pytest.mark.asyncio
async def test_get_active_by_ids_is_one_dataset_scoped_query(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'repo.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(DatasetRecord.__table__.create)
        await connection.run_sync(DocumentRecord.__table__.create)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add_all(
                [
                    DatasetRecord(
                        id="dataset-1",
                        name="d1",
                        provider="p",
                        permission="only_me",
                        indexing_technique="economy",
                        created_by="u",
                    ),
                    _document("active", "dataset-1"),
                    _document("wrong", "dataset-2"),
                    _document(
                        "deleted",
                        "dataset-1",
                        deleted_at=datetime.now(timezone.utc),
                    ),
                ]
            )
            await session.commit()

            records = await DocumentRepository(session).get_active_by_ids(
                "dataset-1", ["active", "wrong", "deleted"]
            )

            assert [record.id for record in records] == ["active"]
    finally:
        await engine.dispose()
