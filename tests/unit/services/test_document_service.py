import hashlib
import io
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from rag_modules.config.settings import UploadSettings
from rag_modules.db.models import DocumentRecord
from rag_modules.services.document_service import DocumentService


def make_upload(filename: str, content: bytes) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": "text/plain"}),
    )


class RecordingStorage:
    def __init__(self, events=None):
        self.keys = []
        self.removed = []
        self.events = events if events is not None else []

    async def ensure_bucket(self):
        pass

    async def put_stream(self, object_key, stream, length, content_type):
        self.events.append("put")
        self.keys.append(object_key)
        assert stream.read() == b"hello"
        return SimpleNamespace(bucket="graph-rag-uploads", object_key=object_key, etag="etag")

    async def remove_object(self, object_key):
        self.removed.append(object_key)


class ExistingDatasetRepository:
    async def get_active(self, dataset_id):
        return SimpleNamespace(id=dataset_id)


class RecordingDocumentRepository:
    def __init__(self, events=None):
        self.created = None
        self.events = events if events is not None else []

    async def find_duplicate(self, dataset_id, sha256, filename):
        return None

    async def next_position(self, dataset_id):
        return 1

    async def create(self, record):
        self.events.append("create")
        self.created = record
        return record


class DatabaseError(RuntimeError):
    pass


class FailingDocumentRepository(RecordingDocumentRepository):
    async def create(self, record):
        raise DatabaseError("database unavailable")


@pytest.mark.asyncio
async def test_upload_one_stores_object_before_committing_document():
    events = []
    storage = RecordingStorage(events)
    repository = RecordingDocumentRepository(events)
    service = DocumentService(repository, ExistingDatasetRepository(), storage, UploadSettings())

    result = await service.upload_one("dataset-1", make_upload("guide.txt", b"hello"), "user-1")

    assert result.status == "waiting"
    assert storage.keys == [f"datasets/dataset-1/documents/{result.id}/source.txt"]
    assert repository.created.data_source_info["storage"] == "minio"
    assert repository.created.data_source_info["original_filename"] == "guide.txt"
    assert repository.created.data_source_info["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert repository.created.data_source_info["etag"] == "etag"
    assert events.index("put") < events.index("create")


@pytest.mark.asyncio
async def test_database_failure_compensates_uploaded_object():
    storage = RecordingStorage()
    service = DocumentService(FailingDocumentRepository(), ExistingDatasetRepository(), storage, UploadSettings())

    with pytest.raises(DatabaseError):
        await service.upload_one("dataset-1", make_upload("guide.txt", b"hello"), "user-1")

    assert storage.removed == storage.keys


@pytest.mark.asyncio
async def test_exact_duplicate_returns_existing_without_uploading():
    storage = RecordingStorage()
    existing = DocumentRecord(
        id="doc-existing", dataset_id="dataset-1", name="guide.txt", position=1,
        data_source_type="upload_file", data_source_info={"sha256": hashlib.sha256(b"hello").hexdigest()},
        created_from="api", created_by="user-1", indexing_status="waiting",
    )

    class DuplicateRepository(RecordingDocumentRepository):
        async def find_duplicate(self, dataset_id, sha256, filename):
            return existing

    service = DocumentService(DuplicateRepository(), ExistingDatasetRepository(), storage, UploadSettings())
    result = await service.upload_one("dataset-1", make_upload("guide.txt", b"hello"), "user-1")

    assert result.id == "doc-existing"
    assert result.duplicate is True
    assert storage.keys == []
