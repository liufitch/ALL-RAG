from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import BinaryIO, Any

import anyio
from minio.error import MinioException, S3Error
from urllib3.exceptions import HTTPError

from .base import ObjectStorageUnavailable, StoredObject


class MinioObjectStorage:
    """Async facade over MinIO's synchronous client."""

    def __init__(self, client: Any, bucket: str):
        self.client = client
        self.bucket = bucket

    async def _run(self, operation, *args):
        try:
            return await anyio.to_thread.run_sync(partial(operation, *args))
        except (MinioException, HTTPError, OSError, TimeoutError, ConnectionError) as exc:
            raise ObjectStorageUnavailable("object storage operation failed") from exc

    async def ensure_bucket(self) -> None:
        exists = await self._run(self.client.bucket_exists, self.bucket)
        if not exists:
            await self._run(self.client.make_bucket, self.bucket)

    async def put_stream(
        self, object_key: str, stream: BinaryIO, length: int, content_type: str
    ) -> StoredObject:
        result = await self._run(
            self.client.put_object,
            self.bucket,
            object_key,
            stream,
            length,
            content_type,
        )
        return StoredObject(
            bucket=self.bucket,
            object_key=object_key,
            etag=getattr(result, "etag", None),
        )

    async def get_bytes(self, object_key: str, max_bytes: int) -> bytes:
        """Download bounded bytes while one worker owns the response lifecycle."""
        try:
            return await anyio.to_thread.run_sync(
                partial(self._get_bytes_sync, object_key, max_bytes),
                abandon_on_cancel=True,
            )
        except (MinioException, HTTPError, OSError, TimeoutError, ConnectionError) as exc:
            raise ObjectStorageUnavailable("object storage operation failed") from exc

    def _get_bytes_sync(self, object_key: str, max_bytes: int) -> bytes:
        response = self.client.get_object(self.bucket, object_key)
        try:
            return response.read(max_bytes)
        finally:
            try:
                response.close()
            finally:
                response.release_conn()

    @asynccontextmanager
    async def get_stream(self, object_key: str) -> AsyncIterator[BinaryIO]:
        response = await self._run(self.client.get_object, self.bucket, object_key)
        try:
            yield response
        finally:
            try:
                await self._run(response.close)
            finally:
                await self._run(response.release_conn)

    async def remove_object(self, object_key: str) -> None:
        try:
            await self._run(self.client.remove_object, self.bucket, object_key)
        except ObjectStorageUnavailable as exc:
            cause = exc.__cause__
            if isinstance(cause, S3Error) and cause.code == "NoSuchKey":
                return
            raise
