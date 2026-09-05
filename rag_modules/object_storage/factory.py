from __future__ import annotations

from functools import lru_cache

from minio import Minio

from rag_modules.config.settings import settings

from .base import ObjectStorage
from .minio_store import MinioObjectStorage


@lru_cache(maxsize=1)
def get_object_storage() -> ObjectStorage:
    """为应用进程返回一个已配置的 MinIO 适配器。

    构造同步 MinIO 客户端仅执行本地操作，不连接服务。
    请求实际使用存储时，网络故障由适配器的异步方法以 ``ObjectStorageUnavailable`` 抛出。
    """
    storage_settings = settings.object_storage
    client = Minio(
        storage_settings.endpoint,
        access_key=storage_settings.access_key.get_secret_value(),
        secret_key=storage_settings.secret_key.get_secret_value(),
        secure=storage_settings.secure,
    )
    return MinioObjectStorage(client=client, bucket=storage_settings.bucket)
