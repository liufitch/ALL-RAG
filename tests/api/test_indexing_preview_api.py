from main import app
from rag_modules.api.indexing_preview_api import get_preview_service
from rag_modules.object_storage import ObjectStorageUnavailable
from rag_modules.services.preview_service import PreviewValidationError


PREVIEW_PATH = "/api/knowledge_base/dataset-1/indexing/preview"


def _general_payload(**segmentation):
    return {
        "document_ids": ["doc-1"],
        "indexing_technique": "economy",
        "segmentation": {"mode": "general", **segmentation},
    }


def test_parent_child_with_economy_is_rejected(client):
    response = client.post(
        PREVIEW_PATH,
        json={
            "document_ids": ["doc-1"],
            "indexing_technique": "economy",
            "segmentation": {"mode": "parent_child"},
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "PARENT_CHILD_REQUIRES_HIGH_QUALITY"
    assert set(response.json()) == {"code", "message"}


def test_preview_success_serializes_real_service_contract(client):
    class StubService:
        async def preview(self, dataset_id, document_ids, request):
            assert dataset_id == "dataset-1"
            assert document_ids == ["doc-1"]
            return {
                "chunks": [
                    {
                        "id": "doc-1:s-000001",
                        "document_id": "doc-1",
                        "local_id": "s-000001",
                        "parent_id": None,
                        "position": 0,
                        "content": "hello",
                        "source_metadata": {"line_start": 1},
                        "index_type": "general",
                    }
                ],
                "total_chunks": 1,
                "truncated": False,
                "warnings": [],
                "documents": [
                    {
                        "document_id": "doc-1",
                        "filename": "note.txt",
                        "source_type": "text",
                        "source_metadata": {"encoding": "utf-8"},
                    }
                ],
            }

    app.dependency_overrides[get_preview_service] = lambda: StubService()
    response = client.post(
        PREVIEW_PATH,
        json=_general_payload(max_chunk_length=10, overlap=0),
    )

    assert response.status_code == 200
    assert response.json()["chunks"][0]["content"] == "hello"
    assert response.json()["documents"][0]["source_metadata"]["encoding"] == "utf-8"


def test_preview_domain_error_uses_top_level_envelope(client):
    class InvalidService:
        async def preview(self, dataset_id, document_ids, request):
            raise PreviewValidationError(
                "INVALID_SOURCE_METADATA", "invalid source"
            )

    app.dependency_overrides[get_preview_service] = lambda: InvalidService()
    response = client.post(PREVIEW_PATH, json=_general_payload())

    assert response.status_code == 422
    assert response.json() == {
        "code": "INVALID_SOURCE_METADATA",
        "message": "invalid source",
    }


def test_preview_storage_failure_is_503(client):
    class UnavailableService:
        async def preview(self, dataset_id, document_ids, request):
            raise ObjectStorageUnavailable("minio down")

    app.dependency_overrides[get_preview_service] = lambda: UnavailableService()
    response = client.post(PREVIEW_PATH, json=_general_payload())

    assert response.status_code == 503
    assert response.json()["code"] == "OBJECT_STORAGE_UNAVAILABLE"


def test_malformed_preview_shape_keeps_standard_fastapi_validation(client):
    response = client.post(
        PREVIEW_PATH,
        json=_general_payload(unexpected=True),
    )

    assert response.status_code == 422
    assert "detail" in response.json()
    assert "code" not in response.json()


def test_preview_dto_does_not_coerce_string_limits(client):
    response = client.post(
        PREVIEW_PATH,
        json=_general_payload(max_chunk_length="10", overlap=0),
    )

    assert response.status_code == 422
    assert "detail" in response.json()


def test_unknown_high_quality_model_is_domain_422_before_database(client):
    response = client.post(
        PREVIEW_PATH,
        json={
            "document_ids": ["doc-1"],
            "indexing_technique": "high_quality",
            "embedding_model": "does-not-exist",
            "segmentation": {"mode": "general"},
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "EMBEDDING_MODEL_UNAVAILABLE"
