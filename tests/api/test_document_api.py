from __future__ import annotations

import io
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from httpx import ASGITransport, AsyncClient
from minio.error import ServerError
from minio.error import S3Error
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers

from main import app
from rag_modules.api.file_api import get_document_service, upload_documents
from rag_modules.config.settings import ObjectStorageSettings, UploadSettings
from rag_modules.db.models import DatasetRecord, DocumentRecord
from rag_modules.documents.types import UploadValidationError
from rag_modules.object_storage import (
    MinioObjectStorage,
    ObjectStorageUnavailable,
    StoredObject,
)
from rag_modules.repositories.document_repository import DocumentRepository
from rag_modules.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag_modules.services.document_service import (
    DatasetNotFoundError,
    DocumentService,
    DocumentUploadItem,
)


class DocumentServiceStub:
    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str]] = []
        self.last_list_arguments: dict[str, object] | None = None
        self.documents = [
            DocumentUploadItem(
                id="doc-1",
                dataset_id="dataset-1",
                name="guide.txt",
                status="waiting",
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
        self.last_list_arguments = {
            "dataset_id": dataset_id,
            "page": page,
            "page_size": page_size,
            "status": status,
            "q": q,
        }
        return self.documents, len(self.documents)


class RecordingStorage:
    def __init__(self, *, fail_on_put: int | None = None) -> None:
        self.fail_on_put = fail_on_put
        self.put_calls: list[str] = []

    async def ensure_bucket(self) -> None:
        return None

    async def put_stream(self, object_key, stream, length, content_type):
        self.put_calls.append(object_key)
        if len(self.put_calls) == self.fail_on_put:
            raise ObjectStorageUnavailable("MinIO unavailable")
        return StoredObject(
            bucket="graph-rag-uploads",
            object_key=object_key,
            etag=f"etag-{len(self.put_calls)}",
        )

    async def remove_object(self, object_key) -> None:
        return None


def make_upload(
    filename: str,
    content: bytes,
    content_type: str = "text/plain",
) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


def make_dataset(
    dataset_id: str,
    *,
    deleted_at: datetime | None = None,
) -> DatasetRecord:
    return DatasetRecord(
        id=dataset_id,
        name=f"Dataset {dataset_id}",
        provider="vendor",
        permission="only_me",
        indexing_technique="high_quality",
        created_by="user-1",
        created_at=datetime.now(timezone.utc),
        deleted_at=deleted_at,
    )


def make_document(
    document_id: str,
    dataset_id: str,
    *,
    position: int,
    name: str,
    status: str,
    deleted_at: datetime | None = None,
) -> DocumentRecord:
    return DocumentRecord(
        id=document_id,
        dataset_id=dataset_id,
        position=position,
        data_source_type="upload_file",
        data_source_info={},
        name=name,
        created_from="api",
        created_by="user-1",
        indexing_status=status,
        deleted_at=deleted_at,
    )


@asynccontextmanager
async def real_document_service(storage, database_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: DatasetRecord.__table__.create(sync_connection)
        )
        await connection.run_sync(
            lambda sync_connection: DocumentRecord.__table__.create(sync_connection)
        )

    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            repository = DocumentRepository(session)
            service = DocumentService(
                repository=repository,
                dataset_repository=KnowledgeBaseRepository(session),
                storage=storage,
                upload_settings=UploadSettings(),
            )
            yield service, session_factory, session
    finally:
        await engine.dispose()


@pytest.fixture
def document_service_stub() -> DocumentServiceStub:
    return DocumentServiceStub()


@pytest.fixture
def document_client(client, document_service_stub):
    # 首次验证测试失败时，不依赖尚未创建的依赖项；
    # 路由存在后，为 API 测试注入结果确定的服务。
    from rag_modules.api import file_api

    dependency = getattr(file_api, "get_document_service", None)
    if dependency is not None:
        app.dependency_overrides[dependency] = lambda: document_service_stub
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


def test_document_list_serializes_service_result(document_client):
    response = document_client.get(
        "/api/knowledge_base/dataset-1/documents?page=1&page_size=20"
    )

    assert response.status_code == 200
    assert all(item["dataset_id"] == "dataset-1" for item in response.json()["items"])
    assert response.json()["total"] == 1


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


def test_document_list_passes_status_and_name_filters_to_service(
    document_client,
    document_service_stub,
):
    response = document_client.get(
        "/api/knowledge_base/dataset-1/documents",
        params={"status": "waiting", "q": "guide"},
    )

    assert response.status_code == 200
    assert document_service_stub.last_list_arguments == {
        "dataset_id": "dataset-1",
        "page": 1,
        "page_size": 20,
        "status": "waiting",
        "q": "guide",
    }


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


@pytest.mark.parametrize("path", ["/documents", "/documents/upload"])
def test_upload_current_and_compatible_paths(document_client, path):
    response = document_client.post(
        f"/api/knowledge_base/dataset-1{path}",
        files=[("files", ("guide.txt", b"hello", "text/plain"))],
    )
    assert response.status_code == 201
    assert response.json()["documents"][0]["name"] == "guide.txt"


@pytest.mark.parametrize("failure", ["storage", "database"])
def test_upload_logs_safe_infrastructure_cause(document_client, caplog, failure):
    class FailingService(DocumentServiceStub):
        async def upload_one(self, dataset_id, file, actor_id):
            if failure == "storage":
                cause = S3Error(response=None, code="AccessDenied", message="sensitive-source-name", resource="/private/key", request_id="req", host_id="host")
                raise ObjectStorageUnavailable("sensitive-config") from cause
            raise OperationalError("private SQL", {"password": "sensitive-config"}, RuntimeError("private db details"))

    app.dependency_overrides[get_document_service] = lambda: FailingService()
    with caplog.at_level(logging.WARNING, logger="rag_modules.api.file_api"):
        response = document_client.post(
            "/api/knowledge_base/dataset-1/documents/upload",
            files=[("files", ("private-filename.txt", b"private payload", "text/plain"))],
        )
    assert response.status_code == 503
    assert "document_upload_failed" in caplog.text
    assert f"component={failure}" in caplog.text
    if failure == "storage":
        assert "AccessDenied" in caplog.text
    for private in ("sensitive-config", "sensitive-source-name", "private-filename", "private SQL", "private db details", "private payload"):
        assert private not in caplog.text
        assert private not in response.text


@pytest.mark.asyncio
async def test_document_upload_maps_minio_server_failure_to_503():
    class ServerFailureClient:
        def bucket_exists(self, bucket):
            raise ServerError("MinIO service unavailable", 503)

    class AdapterBackedUnavailableService(DocumentServiceStub):
        async def upload_one(self, dataset_id, file, actor_id):
            storage = MinioObjectStorage(
                client=ServerFailureClient(),
                bucket="graph-rag-uploads",
            )
            await storage.ensure_bucket()
            raise AssertionError("unreachable")

    app.dependency_overrides[get_document_service] = (
        lambda: AdapterBackedUnavailableService()
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/knowledge_base/dataset-1/documents",
                files=[("files", ("guide.txt", b"hello", "text/plain"))],
            )
    finally:
        app.dependency_overrides.pop(get_document_service, None)

    assert response.status_code == 503


def test_document_list_returns_404_for_missing_dataset(document_client):
    from rag_modules.api.file_api import get_document_service

    class MissingDatasetService(DocumentServiceStub):
        async def list_documents(
            self, dataset_id, *, page, page_size, status=None, q=None
        ):
            raise DatasetNotFoundError(dataset_id)

    app.dependency_overrides[get_document_service] = lambda: MissingDatasetService()
    response = document_client.get("/api/knowledge_base/missing/documents")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_real_document_list_scopes_filters_counts_and_orders_active_records(
    tmp_path,
):
    storage = RecordingStorage()
    async with real_document_service(storage, tmp_path / "list.db") as (
        service,
        _session_factory,
        session,
    ):
        session.add_all(
            [
                make_dataset("dataset-1"),
                make_dataset("dataset-2"),
                make_dataset(
                    "dataset-deleted", deleted_at=datetime.now(timezone.utc)
                ),
                make_document(
                    "doc-c",
                    "dataset-1",
                    position=1,
                    name="Guide introduction.txt",
                    status="waiting",
                ),
                make_document(
                    "doc-b",
                    "dataset-1",
                    position=2,
                    name="Guide beta.txt",
                    status="waiting",
                ),
                make_document(
                    "doc-a",
                    "dataset-1",
                    position=2,
                    name="Guide alpha.txt",
                    status="completed",
                ),
                make_document(
                    "doc-unrelated",
                    "dataset-1",
                    position=0,
                    name="Notes.txt",
                    status="waiting",
                ),
                make_document(
                    "doc-deleted",
                    "dataset-1",
                    position=0,
                    name="Guide deleted.txt",
                    status="waiting",
                    deleted_at=datetime.now(timezone.utc),
                ),
                make_document(
                    "doc-other-dataset",
                    "dataset-2",
                    position=0,
                    name="Guide other dataset.txt",
                    status="waiting",
                ),
            ]
        )
        await session.commit()

        first_page, first_total = await service.list_documents(
            "dataset-1",
            page=1,
            page_size=2,
            q="  guide  ",
        )
        second_page, second_total = await service.list_documents(
            "dataset-1",
            page=2,
            page_size=2,
            q="  guide  ",
        )
        waiting, waiting_total = await service.list_documents(
            "dataset-1",
            page=1,
            page_size=20,
            status="waiting",
            q=" guide ",
        )

        assert [item.id for item in first_page] == ["doc-c", "doc-a"]
        assert first_total == 3
        assert [item.id for item in second_page] == ["doc-b"]
        assert second_total == 3
        assert [item.id for item in waiting] == ["doc-c", "doc-b"]
        assert waiting_total == 2

        with pytest.raises(DatasetNotFoundError):
            await service.list_documents(
                "dataset-deleted", page=1, page_size=20
            )
        with pytest.raises(DatasetNotFoundError):
            await service.list_documents("dataset-missing", page=1, page_size=20)


@pytest.mark.asyncio
async def test_batch_validation_rejection_continues_and_commits_valid_documents(
    tmp_path,
):
    storage = RecordingStorage()
    async with real_document_service(storage, tmp_path / "validation.db") as (
        service,
        session_factory,
        session,
    ):
        session.add(make_dataset("dataset-1"))
        await session.commit()

        app.dependency_overrides[get_document_service] = lambda: service
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    "/api/knowledge_base/dataset-1/documents",
                    files=[
                        ("files", ("first.txt", b"first", "text/plain")),
                        (
                            "files",
                            ("legacy.doc", b"bad", "application/msword"),
                        ),
                        ("files", ("second.txt", b"second", "text/plain")),
                    ],
                )
        finally:
            app.dependency_overrides.pop(get_document_service, None)

        async with session_factory() as verification_session:
            persisted, total = await DocumentRepository(verification_session).list(
                "dataset-1", 1, 20
            )
        payload = response.json()
        assert response.status_code == 201
        assert [item["name"] for item in payload["documents"]] == [
            "first.txt",
            "second.txt",
        ]
        assert [item["code"] for item in payload["rejected"]] == [
            "UNSUPPORTED_FILE_TYPE"
        ]
        assert [record.name for record in persisted] == ["first.txt", "second.txt"]
        assert total == 2


@pytest.mark.asyncio
async def test_batch_storage_failure_returns_503_and_preserves_prior_commit(tmp_path):
    storage = RecordingStorage(fail_on_put=2)
    async with real_document_service(storage, tmp_path / "storage.db") as (
        service,
        session_factory,
        session,
    ):
        session.add(make_dataset("dataset-1"))
        await session.commit()

        with pytest.raises(HTTPException) as error:
            await upload_documents(
                dataset_id="dataset-1",
                files=[
                    make_upload("first.txt", b"first"),
                    make_upload("second.txt", b"second"),
                ],
                service=service,
            )

        async with session_factory() as verification_session:
            persisted, total = await DocumentRepository(verification_session).list(
                "dataset-1", 1, 20
            )
        assert error.value.status_code == 503
        assert [record.name for record in persisted] == ["first.txt"]
        assert total == 1
        assert len(storage.put_calls) == 2


def test_object_storage_factory_wires_config_and_caches(monkeypatch):
    from rag_modules.object_storage import factory as factory_module

    calls: list[dict[str, object]] = []
    client = object()

    def fake_minio(endpoint, *, access_key, secret_key, secure):
        calls.append(
            {
                "endpoint": endpoint,
                "access_key": access_key,
                "secret_key": secret_key,
                "secure": secure,
            }
        )
        return client

    storage_settings = ObjectStorageSettings(
        endpoint="minio.internal:9443",
        access_key="factory-user",
        secret_key="factory-password",
        secure=True,
        bucket="factory-bucket",
    )
    monkeypatch.setattr(
        factory_module,
        "settings",
        SimpleNamespace(object_storage=storage_settings),
    )
    monkeypatch.setattr(factory_module, "Minio", fake_minio)
    factory_module.get_object_storage.cache_clear()
    try:
        first = factory_module.get_object_storage()
        second = factory_module.get_object_storage()
    finally:
        factory_module.get_object_storage.cache_clear()

    assert first is second
    assert calls == [
        {
            "endpoint": "minio.internal:9443",
            "access_key": "factory-user",
            "secret_key": "factory-password",
            "secure": True,
        }
    ]
    assert first.client is client
    assert first.bucket == "factory-bucket"


def test_openapi_exposes_dataset_document_route_without_legacy_upload(document_client):
    paths = document_client.app.openapi()["paths"]

    assert "/api/knowledge_base/{dataset_id}/documents" in paths
    assert "/api/file_manage/upload" not in paths
