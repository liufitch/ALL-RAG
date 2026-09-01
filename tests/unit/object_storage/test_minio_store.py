import io

import pytest
from minio.error import MinioException, S3Error, ServerError
from urllib3.exceptions import NewConnectionError

from rag_modules.object_storage import ObjectStorageUnavailable
from rag_modules.object_storage.minio_store import MinioObjectStorage


class FakeMinioClient:
    def __init__(self):
        self.put_calls = []

    def put_object(self, bucket, object_key, stream, length, content_type):
        self.put_calls.append((bucket, object_key, length, content_type))
        return type("Result", (), {"etag": "etag-1"})()


class MissingObjectClient:
    def remove_object(self, bucket, object_key):
        from minio.error import S3Error

        raise S3Error(
            "No such key",
            "NoSuchKey",
            object_key,
            bucket,
            "request-id",
            "host-id",
        )


class BucketClient:
    def __init__(self, *, exists: bool) -> None:
        self.exists = exists
        self.calls: list[tuple[str, str]] = []

    def bucket_exists(self, bucket: str) -> bool:
        self.calls.append(("bucket_exists", bucket))
        return self.exists

    def make_bucket(self, bucket: str) -> None:
        self.calls.append(("make_bucket", bucket))


class FailingBucketClient:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def bucket_exists(self, bucket: str) -> bool:
        raise self.error


class FakeResponse(io.BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.lifecycle: list[str] = []

    def close(self) -> None:
        self.lifecycle.append("close")
        super().close()

    def release_conn(self) -> None:
        self.lifecycle.append("release_conn")


class GetObjectClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def get_object(self, bucket: str, object_key: str) -> FakeResponse:
        self.calls.append((bucket, object_key))
        return self.response


@pytest.mark.asyncio
async def test_put_stream_uses_configured_bucket_and_exact_length():
    client = FakeMinioClient()
    store = MinioObjectStorage(client=client, bucket="graph-rag-uploads")
    stream = io.BytesIO(b"hello")

    stored = await store.put_stream(
        "datasets/d1/documents/x/source.txt", stream, 5, "text/plain"
    )

    assert client.put_calls == [
        ("graph-rag-uploads", "datasets/d1/documents/x/source.txt", 5, "text/plain")
    ]
    assert stored.object_key.endswith("source.txt")


@pytest.mark.asyncio
async def test_remove_object_is_idempotent_for_missing_key():
    store = MinioObjectStorage(client=MissingObjectClient(), bucket="graph-rag-uploads")
    await store.remove_object("missing")


@pytest.mark.asyncio
async def test_ensure_bucket_creates_configured_bucket_when_missing():
    client = BucketClient(exists=False)
    store = MinioObjectStorage(client=client, bucket="graph-rag-uploads")

    await store.ensure_bucket()

    assert client.calls == [
        ("bucket_exists", "graph-rag-uploads"),
        ("make_bucket", "graph-rag-uploads"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "storage_error",
    [
        ServerError("service unavailable", 503),
        S3Error(
            None,
            "AccessDenied",
            "access denied",
            "/graph-rag-uploads",
            "request-id",
            "host-id",
            "graph-rag-uploads",
        ),
        MinioException("unexpected MinIO response"),
        NewConnectionError(None, "connection refused"),
    ],
    ids=[
        "service-server-error",
        "authentication-s3-error",
        "base-minio-exception",
        "connectivity-error",
    ],
)
async def test_storage_dependency_failures_are_normalized_at_boundary(storage_error):
    store = MinioObjectStorage(
        client=FailingBucketClient(storage_error),
        bucket="graph-rag-uploads",
    )

    with pytest.raises(ObjectStorageUnavailable) as error:
        await store.ensure_bucket()

    assert error.value.__cause__ is storage_error


@pytest.mark.asyncio
async def test_get_stream_closes_and_releases_response_after_normal_exit():
    response = FakeResponse(b"stored content")
    client = GetObjectClient(response)
    store = MinioObjectStorage(client=client, bucket="graph-rag-uploads")

    async with store.get_stream("datasets/d1/documents/x/source.txt") as stream:
        assert stream.read() == b"stored content"

    assert client.calls == [
        ("graph-rag-uploads", "datasets/d1/documents/x/source.txt")
    ]
    assert response.lifecycle == ["close", "release_conn"]


@pytest.mark.asyncio
async def test_get_stream_closes_and_releases_response_after_consumer_error():
    response = FakeResponse(b"stored content")
    store = MinioObjectStorage(
        client=GetObjectClient(response),
        bucket="graph-rag-uploads",
    )

    with pytest.raises(RuntimeError, match="consumer failed"):
        async with store.get_stream("source.txt"):
            raise RuntimeError("consumer failed")

    assert response.lifecycle == ["close", "release_conn"]
