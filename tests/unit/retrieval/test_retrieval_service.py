from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rag_modules.db.base import Base
from rag_modules.db.models import DatasetIndexRecord, DatasetRecord, DocumentRecord, DocumentSegmentRecord
from rag_modules.retrieval import RetrievalService


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_hybrid_retrieval_deduplicates_normalizes_and_filters_threshold(db):
    db.add(DatasetRecord(id="dataset", name="D", provider="p", permission="private", indexing_technique="high_quality", created_by="u"))
    db.add(DatasetIndexRecord(id="index", dataset_id="dataset", created_by_job_id="job", index_type="high_quality", status="active", vector_store_provider="milvus", collection_name="collection", retrieval_config={"semantic_weight": .7, "keyword_weight": .3}, process_rule={}, config_hash="hash"))
    db.add(DocumentRecord(id="doc", dataset_id="dataset", name="file.txt", data_source_type="upload", created_from="api", created_by="u", indexing_status="completed", enabled=True, archived=False))
    db.add_all([
        DocumentSegmentRecord(id="seg1", dataset_id="dataset", document_id="doc", dataset_index_id="index", content="vector and keyword", keywords=["graph"], status="completed", index_type="general", embedding_status="completed", position=0),
        DocumentSegmentRecord(id="seg2", dataset_id="dataset", document_id="doc", dataset_index_id="index", content="weak", keywords=["graph"], status="completed", index_type="general", embedding_status="completed", position=1),
    ])
    await db.commit()

    class V:
        def search(self, *args):
            return [SimpleNamespace(id="seg1", score=.9), SimpleNamespace(id="seg2", score=.1)]
    async def embed(text):
        return (0.1, 0.2)
    service = RetrievalService(db, vector_store=V(), embedder=embed)
    result = await service.retrieve("dataset", "graph", mode="hybrid", top_k=5, score_threshold=.5)
    assert [item.segment_id for item in result.items] == ["seg1"]
    assert result.items[0].match_type == "hybrid"
    assert result.items[0].document_name == "file.txt"
