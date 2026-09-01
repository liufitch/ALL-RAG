from __future__ import annotations

from functools import lru_cache

from minio import Minio

from rag_modules.config.settings import settings

from .base import ObjectStorage
from .minio_store import MinioObjectStorage


@lru_cache(maxsize=1)
def get_object_storage() -> ObjectStorage:
    """Return one configured MinIO adapter for the application process.

    Constructing the synchronous MinIO client is local and does not contact
    the service. Network failures are surfaced by the adapter's async methods
    as ``ObjectStorageUnavailable`` when a request actually uses storage.
    """
    storage_settings = settings.object_storage
    client = Minio(
        storage_settings.endpoint,
        access_key=storage_settings.access_key.get_secret_value(),
        secret_key=storage_settings.secret_key.get_secret_value(),
        secure=storage_settings.secure,
    )
    return MinioObjectStorage(client=client, bucket=storage_settings.bucket)
