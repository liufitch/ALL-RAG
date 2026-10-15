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


def test_parent_context_uses_parent_source_location():
    child = SimpleNamespace(id="child", content="matched words", document_id="doc", parent_id="parent",
                            source_metadata={"page": 3})
    parent = SimpleNamespace(id="parent", content="Full parent context", source_metadata={"page": 2})
    document = SimpleNamespace(name="guide.pdf")

    item = RetrievalService._item((child, document), 0.9, "vector", parent)

    assert item.content == "Full parent context"
    assert item.segment_id == "child"
    assert item.parent_id == "parent"
    assert item.source_metadata == {"page": 2}


@pytest.mark.asyncio
async def test_vector_retrieval_returns_one_context_per_parent_and_fills_top_k(db):
    db.add(DatasetRecord(id="dataset", name="D", provider="p", permission="private", indexing_technique="high_quality", created_by="u"))
    db.add(DatasetIndexRecord(id="index", dataset_id="dataset", created_by_job_id="job", index_type="high_quality", status="active", vector_store_provider="milvus", collection_name="collection", retrieval_config={}, process_rule={}, config_hash="hash"))
    db.add(DocumentRecord(id="doc", dataset_id="dataset", name="guide.txt", data_source_type="upload", created_from="api", created_by="u", indexing_status="completed", enabled=True, archived=False))
    db.add_all([
        DocumentSegmentRecord(id="parent1", dataset_id="dataset", document_id="doc", dataset_index_id="index", content="First parent context", status="completed", index_type="parent", embedding_status="not_required", position=0),
        DocumentSegmentRecord(id="child1", dataset_id="dataset", document_id="doc", dataset_index_id="index", parent_id="parent1", content="first match", status="completed", index_type="child", embedding_status="completed", position=1),
        DocumentSegmentRecord(id="child2", dataset_id="dataset", document_id="doc", dataset_index_id="index", parent_id="parent1", content="second match", status="completed", index_type="child", embedding_status="completed", position=2),
        DocumentSegmentRecord(id="parent2", dataset_id="dataset", document_id="doc", dataset_index_id="index", content="Second parent context", status="completed", index_type="parent", embedding_status="not_required", position=3),
        DocumentSegmentRecord(id="child3", dataset_id="dataset", document_id="doc", dataset_index_id="index", parent_id="parent2", content="third match", status="completed", index_type="child", embedding_status="completed", position=4),
    ])
    await db.commit()

    class VectorStore:
        def search(self, *args):
            return [SimpleNamespace(id="child1", score=.9), SimpleNamespace(id="child2", score=.8),
                    SimpleNamespace(id="child3", score=.7)]

    service = RetrievalService(db, vector_store=VectorStore(), embedder=lambda query: (0.1, 0.2))
    result = await service.retrieve("dataset", "match", mode="vector", top_k=2, score_threshold=0)

    assert [item.segment_id for item in result.items] == ["child1", "child3"]
    assert [item.parent_id for item in result.items] == ["parent1", "parent2"]
    assert [item.content for item in result.items] == ["First parent context", "Second parent context"]


@pytest.mark.asyncio
async def test_retrieval_does_not_use_unfinished_parent_context(db):
    db.add(DatasetRecord(id="dataset", name="D", provider="p", permission="private", indexing_technique="high_quality", created_by="u"))
    db.add_all([
        DatasetIndexRecord(id="active", dataset_id="dataset", created_by_job_id="job1", index_type="high_quality", status="active", vector_store_provider="milvus", collection_name="collection", retrieval_config={}, process_rule={}, config_hash="new"),
        DocumentRecord(id="doc", dataset_id="dataset", name="guide.txt", data_source_type="upload", created_from="api", created_by="u", indexing_status="completed", enabled=True, archived=False),
        DocumentSegmentRecord(id="unfinished-parent", dataset_id="dataset", document_id="doc", dataset_index_id="active", content="Unfinished content", status="indexing", index_type="parent", embedding_status="not_required", position=0),
        DocumentSegmentRecord(id="child", dataset_id="dataset", document_id="doc", dataset_index_id="active", parent_id="unfinished-parent", content="Current child", status="completed", index_type="child", embedding_status="completed", position=1),
    ])
    await db.commit()

    class VectorStore:
        def search(self, *args):
            return [SimpleNamespace(id="child", score=.9)]

    service = RetrievalService(db, vector_store=VectorStore(), embedder=lambda query: (0.1, 0.2))
    result = await service.retrieve("dataset", "current", mode="vector", score_threshold=0)

    assert len(result.items) == 1
    assert result.items[0].content == "Current child"
