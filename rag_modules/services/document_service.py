from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from fastapi import UploadFile

from rag_modules.config.settings import UploadSettings
from rag_modules.db.models import DocumentRecord
from rag_modules.documents.validation import prepare_upload
from rag_modules.object_storage.base import ObjectStorage


class DatasetNotFoundError(LookupError):
    """Raised when an upload targets a missing or soft-deleted dataset."""

    code = "DATASET_NOT_FOUND"


@dataclass(frozen=True)
class DocumentUploadItem:
    id: str
    dataset_id: str
    name: str
    status: str
    duplicate: bool = False


class DocumentService:
    def __init__(
        self,
        repository,
        dataset_repository,
        storage: ObjectStorage,
        upload_settings: UploadSettings,
    ) -> None:
        self.repository = repository
        self.dataset_repository = dataset_repository
        self.storage = storage
        self.upload_settings = upload_settings

    async def upload_one(
        self, dataset_id: str, file: UploadFile, actor_id: str
    ) -> DocumentUploadItem:
        dataset = await self.dataset_repository.get_active(dataset_id)
        if dataset is None:
            raise DatasetNotFoundError(dataset_id)

        prepared = await prepare_upload(file, self.upload_settings)
        duplicate = await self.repository.find_duplicate(
            dataset_id, prepared.sha256, prepared.filename
        )
        if duplicate is not None:
            return self._item(duplicate, duplicate=True)

        document_id = uuid4().hex
        object_key = f"datasets/{dataset_id}/documents/{document_id}/source{prepared.extension}"
        position = await self.repository.next_position(dataset_id)
        ensure_bucket = getattr(self.storage, "ensure_bucket", None)
        if ensure_bucket is not None:
            await ensure_bucket()
        stored = await self.storage.put_stream(
            object_key,
            prepared.stream,
            prepared.size,
            prepared.content_type,
        )

        data_source_info = {
            "storage": "minio",
            "bucket": stored.bucket,
            "object_key": stored.object_key,
            "original_filename": prepared.filename,
            "content_type": prepared.content_type,
            "size": prepared.size,
            "sha256": prepared.sha256,
            "etag": stored.etag,
        }

        record = DocumentRecord(
            id=document_id,
            dataset_id=dataset_id,
            position=position,
            data_source_type="upload_file",
            data_source_info=data_source_info,
            name=prepared.filename,
            created_from="api",
            created_by=actor_id,
            indexing_status="waiting",
        )
        try:
            created = await self.repository.create(record)
        except BaseException:
            # The object is already durable at this point.  Remove precisely
            # that key before propagating any database/commit failure.
            try:
                await self.storage.remove_object(object_key)
            except BaseException:
                # Preserve the original database failure; cleanup can be
                # retried by a maintenance process if storage is unavailable.
                pass
            raise
        return self._item(created)

    async def list_documents(
        self,
        dataset_id: str,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        q: str | None = None,
    ) -> tuple[list[DocumentUploadItem], int]:
        """Return active documents for an active dataset with pagination."""
        dataset = await self.dataset_repository.get_active(dataset_id)
        if dataset is None:
            raise DatasetNotFoundError(dataset_id)

        records, total = await self.repository.list(
            dataset_id,
            page,
            page_size,
            status=status,
            q=q.strip() if q and q.strip() else None,
        )
        return [self._item(record) for record in records], total

    @staticmethod
    def _item(record: DocumentRecord, *, duplicate: bool = False) -> DocumentUploadItem:
        return DocumentUploadItem(
            id=record.id,
            dataset_id=record.dataset_id,
            name=record.name,
            status=record.indexing_status,
            duplicate=duplicate,
        )
