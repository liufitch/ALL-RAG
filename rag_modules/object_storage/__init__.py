"""对象存储的抽象接口与实现。"""

from .base import ObjectStorage, ObjectStorageUnavailable, StoredObject
from .factory import get_object_storage
from .minio_store import MinioObjectStorage

__all__ = [
    "MinioObjectStorage",
    "ObjectStorage",
    "ObjectStorageUnavailable",
    "StoredObject",
    "get_object_storage",
]
