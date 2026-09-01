import io

import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_round_trip_and_remove_real_minio(minio_store, minio_test_prefix):
    key = f"{minio_test_prefix}/source.txt"
    await minio_store.ensure_bucket()
    await minio_store.put_stream(key, io.BytesIO(b"round-trip"), 10, "text/plain")

    async with minio_store.get_stream(key) as stream:
        assert stream.read() == b"round-trip"

    await minio_store.remove_object(key)
    await minio_store.remove_object(key)
