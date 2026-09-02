import time
from threading import Event, Timer

import anyio
import httpx
import pytest
from pydantic import ValidationError
from main import app
from rag_modules.api import indexing_preview_api
from rag_modules.api.dto.indexing_preview import PreviewResponse
from rag_modules.api.dto.indexing_preview import IndexingPreviewRequest
from rag_modules.api.indexing_preview_api import get_preview_service
from rag_modules.config.settings import PreviewSettings, UploadSettings
from rag_modules.db.models import DatasetRecord, DocumentRecord
from rag_modules.db.session import get_db_session
from rag_modules.object_storage import ObjectStorageUnavailable
from rag_modules.object_storage.factory import get_object_storage
from rag_modules.segmentation.segmenter import Segmenter
from rag_modules.services.preview_service import PreviewService
from rag_modules.services.preview_service import PreviewValidationError


PREVIEW_PATH = "/api/knowledge_base/dataset-1/indexing/preview"


def _general_payload(**segmentation):
    return {
        "document_ids": ["doc-1"],
        "indexing_technique": "economy",
        "segmentation": {"mode": "general", **segmentation},
    }


@pytest.mark.parametrize(
    "change",
    [
        {"document_ids": ["d"] * 101},
        {"document_ids": ["d" * 129]},
        {"document_ids": ["   "]},
        {"embedding_model": "m" * 256},
        {"embedding_model": "   "},
        {"segmentation": {"mode": "general", "separator": "s" * 257}},
        {"segmentation": {"mode": "general", "max_chunk_length": 1_000_001}},
        {"segmentation": {"mode": "general", "overlap": 1_000_001}},
        {
            "segmentation": {
                "mode": "parent_child",
                "parent_max_chunk_length": 1_000_001,
            }
        },
        {
            "segmentation": {
                "mode": "parent_child",
                "child_max_chunk_length": 1_000_001,
            }
        },
    ],
)
def test_preview_request_dto_has_finite_string_list_and_numeric_bounds(change):
    """The HTTP model must reject inputs whose validation work or values are unbounded."""
    payload = _general_payload()
    payload.update(change)

    with pytest.raises(ValidationError):
        IndexingPreviewRequest.model_validate(payload)


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
    assert set(response.json()) == {"code", "message", "detail", "request_id"}
    assert response.headers["x-request-id"] == response.json()["request_id"]


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
    assert response.json()["code"] == "INVALID_SOURCE_METADATA"
    assert response.json()["message"] == "invalid source"
    assert response.json()["detail"] is None
    assert response.headers["x-request-id"] == response.json()["request_id"]


def test_preview_storage_failure_is_503(client):
    class UnavailableService:
        async def preview(self, dataset_id, document_ids, request):
            raise ObjectStorageUnavailable("minio down")

    app.dependency_overrides[get_preview_service] = lambda: UnavailableService()
    response = client.post(PREVIEW_PATH, json=_general_payload())

    assert response.status_code == 503
    assert response.json()["code"] == "OBJECT_STORAGE_UNAVAILABLE"
    assert set(response.json()) == {"code", "message", "detail", "request_id"}
    assert response.headers["x-request-id"] == response.json()["request_id"]


def test_malformed_preview_shape_uses_sanitized_preview_validation_envelope(client):
    response = client.post(
        PREVIEW_PATH,
        json=_general_payload(unexpected=True),
        headers={"X-Request-ID": "preview-validation-1"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "PREVIEW_REQUEST_VALIDATION_FAILED"
    assert response.json()["request_id"] == "preview-validation-1"
    assert response.headers["x-request-id"] == "preview-validation-1"
    assert response.json()["detail"] == [
        {
            "location": ["body", "segmentation", "general", "unexpected"],
            "message": "Extra inputs are not permitted",
            "type": "extra_forbidden",
        }
    ]


def test_preview_dto_does_not_coerce_string_limits(client):
    response = client.post(
        PREVIEW_PATH,
        json=_general_payload(max_chunk_length="10", overlap=0),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "PREVIEW_REQUEST_VALIDATION_FAILED"


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


def test_explicit_empty_high_quality_model_is_domain_422(client):
    response = client.post(
        PREVIEW_PATH,
        json={
            "document_ids": ["doc-1"],
            "indexing_technique": "high_quality",
            "embedding_model": "",
            "segmentation": {"mode": "general"},
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "PREVIEW_REQUEST_VALIDATION_FAILED"


def test_preview_validation_detail_never_echoes_secret_input(client):
    secret = "secret-value-that-must-not-leak"
    response = client.post(
        PREVIEW_PATH,
        json={**_general_payload(), "embedding_model": secret * 100},
    )

    assert response.status_code == 422
    assert secret not in response.text


def test_preview_rejects_unbounded_dataset_identifier_with_same_envelope(client):
    response = client.post(
        "/api/knowledge_base/" + "d" * 129 + "/indexing/preview",
        json=_general_payload(),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "PREVIEW_REQUEST_VALIDATION_FAILED"
    assert response.headers["x-request-id"] == response.json()["request_id"]


def test_preview_openapi_declares_one_complete_error_envelope(client):
    operation = client.get("/openapi.json").json()["paths"][PREVIEW_PATH.replace("dataset-1", "{dataset_id}")]["post"]
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    for status in ("404", "422", "503", "504"):
        schema = operation["responses"][status]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith("/PreviewErrorResponse")
        assert "X-Request-ID" in operation["responses"][status]["headers"]
    assert set(schemas["PreviewErrorResponse"]["required"]) == {
        "code",
        "message",
        "detail",
        "request_id",
    }


def test_preview_rendering_is_inside_route_deadline(client, monkeypatch):
    class StubService:
        async def preview(self, dataset_id, document_ids, request):
            return {
                "chunks": [],
                "total_chunks": 0,
                "truncated": False,
                "warnings": [],
                "documents": [],
            }

    entered, release, finished = Event(), Event(), Event()
    original_dump = PreviewResponse.model_dump

    def slow_dump(response, *args, **kwargs):
        entered.set()
        release.wait(timeout=5)
        try:
            return original_dump(response, *args, **kwargs)
        finally:
            finished.set()

    app.dependency_overrides[get_preview_service] = lambda: StubService()
    monkeypatch.setattr(PreviewResponse, "model_dump", slow_dump)
    monkeypatch.setattr(
        indexing_preview_api.settings,
        "preview",
        PreviewSettings(max_documents=20, max_chunks=100, timeout_seconds=1),
    )
    delayed_release = Timer(1.6, release.set)
    delayed_release.start()
    started = time.monotonic()

    try:
        response = client.post(PREVIEW_PATH, json=_general_payload())
        assert response.status_code == 504
        assert response.json()["code"] == "PREVIEW_TIMEOUT"
        assert time.monotonic() - started < 1.4
        assert entered.is_set()
    finally:
        release.set()
        delayed_release.cancel()
        delayed_release.join(timeout=1)

    assert finished.wait(timeout=1)


def test_dependency_timeout_is_still_infrastructure_503(client):
    class TimedOutService:
        async def preview(self, dataset_id, document_ids, request):
            raise TimeoutError("database timed out")

    app.dependency_overrides[get_preview_service] = lambda: TimedOutService()
    response = client.post(PREVIEW_PATH, json=_general_payload())

    assert response.status_code == 503
    assert response.json()["code"] == "PREVIEW_INFRASTRUCTURE_UNAVAILABLE"
    assert set(response.json()) == {"code", "message", "detail", "request_id"}
    assert response.headers["x-request-id"] == response.json()["request_id"]


def test_slow_storage_is_a_bounded_api_504(client):
    entered, release, finished = Event(), Event(), Event()

    class Documents:
        async def get_active_by_ids(self, dataset_id, document_ids):
            return [
                DocumentRecord(
                    id="doc-1",
                    dataset_id="dataset-1",
                    position=1,
                    data_source_type="upload_file",
                    data_source_info={
                        "storage": "minio",
                        "bucket": "graph-rag-uploads",
                        "object_key": (
                            "datasets/dataset-1/documents/doc-1/source.txt"
                        ),
                        "original_filename": "note.txt",
                        "size": 1,
                    },
                    name="note.txt",
                    created_from="api",
                    created_by="user-1",
                    indexing_status="waiting",
                )
            ]

    class Dataset:
        async def get_active(self, dataset_id):
            return object()

    class Storage:
        def download(self, max_bytes):
            entered.set()
            release.wait(timeout=5)
            finished.set()
            return b"A"[:max_bytes]

        async def get_bytes(self, object_key, max_bytes):
            return await anyio.to_thread.run_sync(
                self.download,
                max_bytes,
                abandon_on_cancel=True,
            )

    service = PreviewService(
        repository=Documents(),
        dataset_repository=Dataset(),
        storage=Storage(),
        parser_registry=object(),
        segmenter=Segmenter(),
        preview_settings=PreviewSettings(
            max_documents=20,
            max_chunks=100,
            timeout_seconds=1,
        ),
        upload_settings=UploadSettings(),
    )
    app.dependency_overrides[get_preview_service] = lambda: service
    delayed_release = Timer(1.6, release.set)
    delayed_release.start()
    started = time.monotonic()

    try:
        response = client.post(PREVIEW_PATH, json=_general_payload())
        assert response.status_code == 504
        assert response.json()["code"] == "PREVIEW_TIMEOUT"
        assert time.monotonic() - started < 1.4
        assert entered.is_set()
    finally:
        release.set()
        delayed_release.cancel()
        delayed_release.join(timeout=1)

    assert finished.wait(timeout=1)


def test_production_preview_composition_is_read_only_and_never_constructs_vector_clients(
    client, monkeypatch
):
    """The real route factory must stop at parse/segment and issue SELECT-only SQL."""
    import rag_modules.vector_stores.factory as vector_factory
    import rag_modules.vector_stores.milvus as milvus_module

    record = DocumentRecord(
        id="doc-1",
        dataset_id="dataset-1",
        position=1,
        data_source_type="upload_file",
        data_source_info={
            "storage": "minio",
            "bucket": "graph-rag-uploads",
            "object_key": "datasets/dataset-1/documents/doc-1/source.txt",
            "original_filename": "note.txt",
            "size": 1,
        },
        name="note.txt",
        created_from="api",
        created_by="user-1",
        indexing_status="waiting",
    )

    class Result:
        def __init__(self, values):
            self.values = values

        def scalar_one_or_none(self):
            return self.values[0] if self.values else None

        def scalars(self):
            return iter(self.values)

    class SelectOnlySession:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            assert statement.is_select
            sql = str(statement)
            assert "document_segments" not in sql
            self.statements.append(sql)
            entity = statement.column_descriptions[0].get("entity")
            if entity is DatasetRecord:
                return Result([object()])
            if entity is DocumentRecord:
                return Result([record])
            raise AssertionError(f"unexpected preview SQL: {sql}")

        def add(self, _record):
            raise AssertionError("preview attempted segment persistence")

        async def commit(self):
            raise AssertionError("preview attempted a database commit")

    class Storage:
        async def get_bytes(self, object_key, max_bytes):
            assert object_key == record.data_source_info["object_key"]
            assert max_bytes == 2
            return b"A"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("preview constructed an Embedding/Milvus/vector client")

    session = SelectOnlySession()
    monkeypatch.setattr(vector_factory, "get_vector_store", forbidden)
    monkeypatch.setattr(milvus_module, "MilvusClient", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    app.dependency_overrides[get_db_session] = lambda: session
    app.dependency_overrides[get_object_storage] = lambda: Storage()

    response = client.post(
        PREVIEW_PATH,
        json={
            "document_ids": ["doc-1"],
            "indexing_technique": "high_quality",
            "embedding_model": "bge-m3",
            "segmentation": {
                "mode": "general",
                "max_chunk_length": 10,
                "overlap": 0,
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["chunks"][0]["content"] == "A"
    assert len(session.statements) == 2
