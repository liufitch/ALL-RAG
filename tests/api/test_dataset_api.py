from __future__ import annotations

from datetime import datetime, timezone

import celery
import httpx
import minio
import pytest

from main import app
from rag_modules.api.dto.knowledge_base.knowledgeBaseCreate import KnowledgeBaseCreate
from rag_modules.api.knowledge_base_api import get_knowledge_base_service
from rag_modules.db.models import DatasetRecord
from rag_modules.services import knowledge_base_service as service_module
from rag_modules.services.knowledge_base_service import KnowledgeBaseService
from rag_modules.vector_stores import factory as vector_store_factory


class DatasetRepositoryStub:
    def __init__(self) -> None:
        self.records: dict[str, DatasetRecord] = {}
        self.counts: dict[str, tuple[int, int]] = {}
        self.last_list_filters: dict[str, object] | None = None

    async def create(self, record: DatasetRecord) -> DatasetRecord:
        now = datetime.now(timezone.utc)
        record.created_at = now
        record.updated_at = None
        self.records[record.id] = record
        return record

    async def get_active(self, dataset_id: str) -> DatasetRecord | None:
        record = self.records.get(dataset_id)
        if record is None or record.deleted_at is not None:
            return None
        return record

    async def get_active_with_counts(
        self,
        dataset_id: str,
    ) -> tuple[DatasetRecord, int, int] | None:
        record = await self.get_active(dataset_id)
        if record is None:
            return None
        document_count, chunk_count = self.counts.get(dataset_id, (0, 0))
        return record, document_count, chunk_count

    async def list(
        self,
        page: int,
        page_size: int,
        *,
        status: str = "all",
        visibility: str = "all",
        q: str | None = None,
    ) -> tuple[list[tuple[DatasetRecord, int, int]], int]:
        self.last_list_filters = {
            "page": page,
            "page_size": page_size,
            "status": status,
            "visibility": visibility,
            "q": q,
        }
        return [], 0


@pytest.fixture
def repository() -> DatasetRepositoryStub:
    return DatasetRepositoryStub()


@pytest.fixture
def dataset_client(client, repository):
    app.dependency_overrides[get_knowledge_base_service] = lambda: KnowledgeBaseService(repository)
    return client


def test_create_empty_dataset_returns_201_without_vector_configuration(
    dataset_client,
    repository,
) -> None:
    response = dataset_client.post(
        "/api/knowledge_base",
        json={"name": "产品知识库", "description": "说明", "permission": "only_me"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["name"] == "产品知识库"
    assert payload["permission"] == "only_me"
    assert payload["indexing_status"] == "not_started"
    assert "vector_store" not in payload
    assert "retrieval_config" not in payload
    assert payload.get("embedding_model") is None

    [record] = repository.records.values()
    assert record.provider == "vendor"
    assert record.indexing_technique == "high_quality"
    assert record.embedding_model is None
    assert record.embedding_model_provider is None
    assert record.retrieval_model_config is None
    assert record.partial_user_config == {"process_rule": None}


@pytest.mark.parametrize(
    ("legacy_field", "legacy_value"),
    [
        ("embedding_model", "bge-m3"),
        ("retrieval_config", {"mode": "vector"}),
        ("vector_store", {"provider": "milvus"}),
        ("visibility", "private"),
    ],
)
def test_create_rejects_legacy_configuration_fields(
    dataset_client,
    legacy_field: str,
    legacy_value: object,
) -> None:
    response = dataset_client.post(
        "/api/knowledge_base",
        json={"name": "产品知识库", legacy_field: legacy_value},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_service_never_calls_external_providers(
    repository,
    monkeypatch,
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("external providers must not be called while creating an empty dataset")

    monkeypatch.setattr(service_module, "get_vector_store", forbidden, raising=False)
    monkeypatch.setattr(vector_store_factory, "get_vector_store", forbidden)
    monkeypatch.setattr(minio, "Minio", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "post", forbidden)
    monkeypatch.setattr(celery.Celery, "send_task", forbidden)

    created = await KnowledgeBaseService(repository).create_knowledge_base(
        KnowledgeBaseCreate(name="产品知识库", permission="only_me")
    )

    assert created.indexing_status == "not_started"


def test_get_dataset_returns_active_dataset(dataset_client, repository) -> None:
    now = datetime.now(timezone.utc)
    repository.records["dataset-1"] = DatasetRecord(
        id="dataset-1",
        name="Dataset One",
        provider="vendor",
        permission="all_team_members",
        indexing_technique="high_quality",
        created_by="user-1",
        created_at=now,
        partial_user_config={"process_rule": None},
    )

    response = dataset_client.get("/api/knowledge_base/dataset-1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "dataset-1"
    assert payload["permission"] == "all_team_members"
    assert payload["document_count"] == 0
    assert payload["chunk_count"] == 0
    assert payload["indexing_status"] == "not_started"
    assert payload["status"] == "draft"


def test_get_non_empty_dataset_returns_active_counts_and_ready_status(
    dataset_client,
    repository,
) -> None:
    repository.records["dataset-1"] = DatasetRecord(
        id="dataset-1",
        name="Indexed Dataset",
        provider="vendor",
        permission="only_me",
        indexing_technique="high_quality",
        created_by="user-1",
        created_at=datetime.now(timezone.utc),
    )
    repository.counts["dataset-1"] = (2, 7)

    response = dataset_client.get("/api/knowledge_base/dataset-1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["document_count"] == 2
    assert payload["chunk_count"] == 7
    assert payload["indexing_status"] == "completed"
    assert payload["status"] == "ready"


@pytest.mark.parametrize("deleted", [False, True])
def test_get_dataset_returns_404_for_missing_or_deleted_dataset(
    dataset_client,
    repository,
    deleted: bool,
) -> None:
    if deleted:
        repository.records["dataset-1"] = DatasetRecord(
            id="dataset-1",
            name="Deleted Dataset",
            provider="vendor",
            permission="only_me",
            indexing_technique="high_quality",
            created_by="user-1",
            created_at=datetime.now(timezone.utc),
            deleted_at=datetime.now(timezone.utc),
        )

    response = dataset_client.get("/api/knowledge_base/dataset-1")

    assert response.status_code == 404


def test_list_accepts_and_passes_compatible_filters(dataset_client, repository) -> None:
    response = dataset_client.get(
        "/api/knowledge_base/list",
        params={
            "status": "draft",
            "visibility": "team",
            "q": "产品",
            "page": 2,
            "page_size": 25,
        },
    )

    assert response.status_code == 200
    assert repository.last_list_filters == {
        "status": "draft",
        "visibility": "team",
        "q": "产品",
        "page": 2,
        "page_size": 25,
    }


@pytest.mark.parametrize(
    ("query_name", "query_value"),
    [("status", "unknown"), ("visibility", "unknown")],
)
def test_list_rejects_unknown_filter_values(
    dataset_client,
    query_name: str,
    query_value: str,
) -> None:
    response = dataset_client.get(
        "/api/knowledge_base/list",
        params={query_name: query_value},
    )

    assert response.status_code == 422
