from types import SimpleNamespace

import pytest

from rag_modules.db.models import DatasetIndexRecord, DatasetRecord, DocumentRecord, DocumentSegmentRecord
from rag_modules.retrieval import RetrievalService
from tests.unit.retrieval.test_retrieval_service import db


@pytest.mark.asyncio
async def test_graph_retrieval_uses_active_index_and_filters_disabled_documents(db):
    db.add(DatasetRecord(id="dataset", name="D", provider="p", permission="private", indexing_technique="economy", created_by="u"))
    db.add(DatasetIndexRecord(id="index", dataset_id="dataset", created_by_job_id="job", index_type="economy", status="active", vector_store_provider="milvus", retrieval_config={}, process_rule={}, config_hash="hash"))
    db.add_all([
        DocumentRecord(id="live", dataset_id="dataset", name="live.txt", data_source_type="upload", created_from="api", created_by="u", indexing_status="completed", enabled=True, archived=False),
        DocumentRecord(id="disabled", dataset_id="dataset", name="disabled.txt", data_source_type="upload", created_from="api", created_by="u", indexing_status="completed", enabled=False, archived=False),
    ])
    db.add_all([
        DocumentSegmentRecord(id="seg-live", dataset_id="dataset", document_id="live", dataset_index_id="index", content="腾讯位于深圳", status="completed", index_type="general", embedding_status="not_required", position=0),
        DocumentSegmentRecord(id="seg-disabled", dataset_id="dataset", document_id="disabled", dataset_index_id="index", content="隐藏内容", status="completed", index_type="general", embedding_status="not_required", position=0),
    ])
    await db.commit()

    class Graph:
        def search(self, index_id, query, limit):
            assert index_id == "index"
            return [SimpleNamespace(id="seg-live", score=1), SimpleNamespace(id="seg-disabled", score=1)]

    result = await RetrievalService(db, graph_store=Graph()).retrieve("dataset", "腾讯", mode="graph", score_threshold=0)
    assert [item.segment_id for item in result.items] == ["seg-live"]
    assert result.items[0].match_type == "graph"
