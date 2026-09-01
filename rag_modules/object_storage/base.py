from dataclasses import dataclass
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import BinaryIO, Protocol


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    object_key: str
    etag: str | None


class ObjectStorageUnavailable(RuntimeError):
    """The object-storage service could not complete an operation."""


class ObjectStorage(Protocol):
    async def ensure_bucket(self) -> None: ...

    async def put_stream(
        self, object_key: str, stream: BinaryIO, length: int, content_type: str
    ) -> StoredObject: ...

    @asynccontextmanager
    async def get_stream(self, object_key: str) -> AsyncIterator[BinaryIO]: ...

    async def remove_object(self, object_key: str) -> None: ...
