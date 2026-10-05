from datetime import datetime, timezone

import pytest

from main import app
from rag_modules.api.dto.knowledge_base.settings import KnowledgeBaseSettingsResponse, RetrievalConfig
from rag_modules.db.models import DatasetRecord
from rag_modules.services.knowledge_base_service import KnowledgeBaseService


class Repository:
    def __init__(self):
        self.record = DatasetRecord(
            id="dataset-1", name="知识库", description="说明", provider="vendor",
            permission="only_me", indexing_technique="high_quality", created_by="u1",
            created_at=datetime.now(timezone.utc),
        )

    async def get_active(self, dataset_id):
        return self.record if dataset_id == self.record.id else None

    async def update_settings(self, record, **values):
        for key, value in values.items():
            setattr(record, key, value)
        return record


class Publisher:
    def __init__(self):
        self.jobs = []

    def dispatch_job_document(self, *, job_document_id):
        self.jobs.append(job_document_id)
        return "task-1"


class Service(KnowledgeBaseService):
    async def rebuild(self, dataset_id, actor_id):
        if dataset_id != "dataset-1":
            return None
        return {"job_id": "job-1", "status": "queued"}


@pytest.fixture
def settings_client(client):
    from rag_modules.api import knowledge_base_settings_api

    repository = Repository()
    publisher = Publisher()
    app.dependency_overrides[knowledge_base_settings_api.get_knowledge_base_settings_service] = lambda: Service(repository)
    app.dependency_overrides[knowledge_base_settings_api.get_task_publisher] = lambda: publisher
    yield client
    app.dependency_overrides.clear()


def test_settings_get_and_patch(settings_client):
    response = settings_client.get("/api/knowledge_base/dataset-1/settings")
    assert response.status_code == 200
    assert response.json()["retrieval"]["mode"] == "hybrid"

    response = settings_client.patch(
        "/api/knowledge_base/dataset-1/settings",
        json={"name": "新名称", "retrieval": {"top_k": 8}},
    )
    assert response.status_code == 200
    assert response.json()["dataset"]["name"] == "新名称"


def test_settings_get_missing_dataset_returns_404(settings_client):
    assert settings_client.get("/api/knowledge_base/missing/settings").status_code == 404


def test_rebuild_returns_job(settings_client):
    response = settings_client.post("/api/knowledge_base/dataset-1/rebuild")
    assert response.status_code == 202
    assert response.json() == {"job_id": "job-1", "status": "queued"}
