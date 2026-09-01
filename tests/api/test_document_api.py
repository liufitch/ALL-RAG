from __future__ import annotations

import pytest

from main import app
from rag_modules.documents.types import UploadValidationError
from rag_modules.object_storage import ObjectStorageUnavailable
from rag_modules.services.document_service import (
    DatasetNotFoundError,
    DocumentUploadItem,
)


class DocumentServiceStub:
    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str]] = []
        self.documents = [
            DocumentUploadItem(
                id="doc-1",
                dataset_id="dataset-1",
                name="guide.txt",
                status="waiting",
            ),
            DocumentUploadItem(
                id="doc-2",
                dataset_id="dataset-2",
                name="other.txt",
                status="completed",
            ),
        ]

    async def upload_one(self, dataset_id, file, actor_id):
        if dataset_id != "dataset-1":
            raise DatasetNotFoundError(dataset_id)
        if (file.filename or "").lower().endswith((".doc", ".pptx")):
            raise UploadValidationError(
                "UNSUPPORTED_FILE_TYPE", "The file extension is not supported."
            )
        self.uploaded.append((dataset_id, file.filename or ""))
        return DocumentUploadItem(
            id=f"doc-{len(self.uploaded) + 2}",
            dataset_id=dataset_id,
            name=file.filename or "",
            status="waiting",
        )

    async def list_documents(self, dataset_id, *, page, page_size, status=None, q=None):
        if dataset_id != "dataset-1":
            raise DatasetNotFoundError(dataset_id)
        items = [item for item in self.documents if item.dataset_id == dataset_id]
        if status:
            items = [item for item in items if item.status == status]
        if q:
            items = [item for item in items if q.lower() in item.name.lower()]
        total = len(items)
        start = (page - 1) * page_size
        return items[start : start + page_size], total


@pytest.fixture
def document_client(client):
    # Keep the initial RED run independent of the not-yet-created dependency;
    # once the route exists, inject a deterministic service for API tests.
    from rag_modules.api import file_api

    service = DocumentServiceStub()
    dependency = getattr(file_api, "get_document_service", None)
    if dependency is not None:
        app.dependency_overrides[dependency] = lambda: service
    yield client
    app.dependency_overrides.clear()


def test_batch_upload_returns_successes_and_rejections(document_client):
    response = document_client.post(
        "/api/knowledge_base/dataset-1/documents",
        files=[
            ("files", ("guide.txt", b"hello", "text/plain")),
            ("files", ("legacy.doc", b"bad", "application/msword")),
        ],
    )

    assert response.status_code == 201
    assert [item["name"] for item in response.json()["documents"]] == ["guide.txt"]
    assert response.json()["rejected"][0]["code"] == "UNSUPPORTED_FILE_TYPE"


def test_document_list_is_scoped_to_dataset(document_client):
    response = document_client.get(
        "/api/knowledge_base/dataset-1/documents?page=1&page_size=20"
    )

    assert response.status_code == 200
    assert all(item["dataset_id"] == "dataset-1" for item in response.json()["items"])
    assert response.json()["total"] == 1


def test_batch_upload_keeps_prior_success_when_one_file_is_invalid(document_client):
    response = document_client.post(
        "/api/knowledge_base/dataset-1/documents",
        files=[
            ("files", ("first.txt", b"first", "text/plain")),
            ("files", ("bad.pptx", b"bad", "application/octet-stream")),
            ("files", ("second.txt", b"second", "text/plain")),
        ],
    )

    assert response.status_code == 201
    assert [item["name"] for item in response.json()["documents"]] == [
        "first.txt",
        "second.txt",
    ]
    assert [item["filename"] for item in response.json()["rejected"]] == ["bad.pptx"]


@pytest.mark.parametrize(
    ("query", "expected_status"),
    [
        ({"page": "0"}, 422),
        ({"page_size": "0"}, 422),
        ({"page_size": "101"}, 422),
        ({"q": "x" * 256}, 422),
    ],
)
def test_document_list_validates_pagination_and_query_filters(
    document_client, query, expected_status
):
    response = document_client.get(
        "/api/knowledge_base/dataset-1/documents", params=query
    )

    assert response.status_code == expected_status


def test_document_list_applies_status_and_name_filters(document_client):
    response = document_client.get(
        "/api/knowledge_base/dataset-1/documents",
        params={"status": "waiting", "q": "guide"},
    )

    assert response.status_code == 200
    assert [item["name"] for item in response.json()["items"]] == ["guide.txt"]


def test_document_upload_maps_storage_unavailability_to_503(document_client):
    from rag_modules.api.file_api import get_document_service

    class UnavailableService(DocumentServiceStub):
        async def upload_one(self, dataset_id, file, actor_id):
            raise ObjectStorageUnavailable("MinIO unavailable")

    app.dependency_overrides[get_document_service] = lambda: UnavailableService()
    response = document_client.post(
        "/api/knowledge_base/dataset-1/documents",
        files=[("files", ("guide.txt", b"hello", "text/plain"))],
    )

    assert response.status_code == 503


def test_document_list_returns_404_for_missing_dataset(document_client):
    response = document_client.get("/api/knowledge_base/missing/documents")

    assert response.status_code == 404


def test_object_storage_dependency_uses_cached_factory():
    from rag_modules.object_storage.factory import get_object_storage

    get_object_storage.cache_clear()
    first = get_object_storage()
    second = get_object_storage()
    get_object_storage.cache_clear()

    assert first is second


def test_openapi_exposes_dataset_document_route_without_legacy_upload(document_client):
    paths = document_client.app.openapi()["paths"]

    assert "/api/knowledge_base/{dataset_id}/documents" in paths
    assert "/api/file_manage/upload" not in paths
