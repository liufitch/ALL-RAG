from datetime import datetime, timezone

import pytest

from rag_modules.api.dto.knowledge_base.settings import KnowledgeBaseSettingsUpdate
from rag_modules.db.models import DatasetRecord
from rag_modules.services.knowledge_base_service import KnowledgeBaseService


class SettingsRepository:
    def __init__(self, record):
        self.record = record

    async def get_active(self, dataset_id):
        return self.record if self.record and self.record.id == dataset_id else None

    async def update_settings(self, record, **values):
        for key, value in values.items():
            setattr(record, key, value)
        return record


def dataset(**kwargs):
    values = dict(
        id="dataset-1", name="知识库", description="说明", provider="vendor",
        permission="only_me", indexing_technique="high_quality", created_by="u1",
        created_at=datetime.now(timezone.utc), partial_user_config=None,
        retrieval_model_config=None,
    )
    values.update(kwargs)
    return DatasetRecord(**values)


@pytest.mark.asyncio
async def test_get_settings_supplies_defaults_for_legacy_null_config():
    result = await KnowledgeBaseService(SettingsRepository(dataset())).get_settings("dataset-1")

    assert result.retrieval.mode == "hybrid"
    assert result.retrieval.top_k == 5
    assert result.retrieval.semantic_weight == 0.7
    assert result.needs_rebuild is False


@pytest.mark.asyncio
async def test_update_settings_persists_retrieval_and_marks_index_changes():
    record = dataset()
    service = KnowledgeBaseService(SettingsRepository(record))
    result = await service.update_settings(
        "dataset-1",
        KnowledgeBaseSettingsUpdate(
            name="新名称",
            indexing_technique="economy",
            retrieval={"mode": "vector", "top_k": 8, "semantic_weight": 1, "keyword_weight": 0},
        ),
        "u1",
    )

    assert record.name == "新名称"
    assert record.indexing_technique == "economy"
    assert record.retrieval_model_config["top_k"] == 8
    assert result.needs_rebuild is True


def test_retrieval_weights_must_sum_to_one():
    with pytest.raises(ValueError):
        KnowledgeBaseSettingsUpdate(
            retrieval={"semantic_weight": 0.2, "keyword_weight": 0.2}
        )
