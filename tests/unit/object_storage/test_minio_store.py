import io

import pytest

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


@pytest.mark.asyncio
async def test_put_stream_uses_configured_bucket_and_exact_length(monkeypatch):
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
