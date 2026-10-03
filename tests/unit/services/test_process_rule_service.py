from __future__ import annotations

from types import SimpleNamespace

import pytest


def request(*, document_ids=("doc-1",), technique="high_quality", mode="general"):
    return SimpleNamespace(
        document_ids=list(document_ids),
        indexing_technique=technique,
        embedding_model="bge-m3" if technique == "high_quality" else None,
        segmentation=SimpleNamespace(
            mode=mode,
            max_chunk_length=800,
            overlap=80,
            separator="\n",
        ) if mode == "general" else SimpleNamespace(
            mode=mode,
            parent_mode="paragraph",
            parent_max_chunk_length=3000,
            child_max_chunk_length=400,
            child_overlap=40,
            separator="\n",
        ),
    )


class FakeRepository:
    def __init__(self):
        self.documents = [SimpleNamespace(id="doc-1", dataset_id="dataset-1", deleted_at=None, dataset_process_rule_id=None)]
        self.rules = []
        self.jobs = []
        self.job_documents = []
        self.commits = 0

    async def get_active_documents(self, dataset_id, document_ids):
        return [item for item in self.documents if item.dataset_id == dataset_id and item.id in document_ids and item.deleted_at is None]

    async def get_current_rule(self, dataset_id, snapshot):
        return None

    async def find_rule(self, dataset_id, config_hash):
        return next((rule for rule in self.rules if rule.config_hash == config_hash), None)

    async def create_rule_and_job(self, **kwargs):
        if kwargs.get("rule_is_new", True):
            self.rules.append(kwargs["rule"])
        self.jobs.append(kwargs["job"])
        self.job_documents.extend(kwargs["job_documents"])
        for document in kwargs["documents"]:
            document.dataset_process_rule_id = kwargs["rule"].id
        self.commits += 1
        return kwargs["rule"], kwargs["job"], kwargs["job_documents"]


@pytest.mark.asyncio
async def test_confirm_creates_process_rule_binds_documents_and_snapshots_job_config():
    from rag_modules.services.process_rule_service import ProcessRuleService

    repository = FakeRepository()
    service = ProcessRuleService(repository)

    result = await service.confirm("dataset-1", request(), actor_id="user-1")

    assert result.rule.id == repository.documents[0].dataset_process_rule_id
    assert result.rule.mode == "general"
    assert result.rule.rules["max_chunk_length"] == 800
    assert result.job.process_rule["segmentation"]["overlap"] == 80
    assert result.job_documents[0].document_id == "doc-1"
    assert repository.commits == 1


@pytest.mark.asyncio
async def test_confirm_same_configuration_is_idempotent():
    from rag_modules.services.process_rule_service import ProcessRuleService

    repository = FakeRepository()
    repository.get_current_rule = lambda dataset_id, snapshot: None
    service = ProcessRuleService(repository)

    first = await service.confirm("dataset-1", request(), actor_id="user-1")
    second = await service.confirm("dataset-1", request(), actor_id="user-1")

    assert first.rule.id == second.rule.id
    assert len(repository.rules) == 1
