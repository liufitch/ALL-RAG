import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from minio import Minio
from minio.error import InvalidResponseError, S3Error
from urllib3.exceptions import HTTPError

from main import app
from rag_modules.config.settings import settings
from rag_modules.object_storage import MinioObjectStorage
from rag_modules.vector_stores.milvus import MilvusVectorStore


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: test requires live infrastructure")
    config.addinivalue_line("markers", "e2e: end-to-end test")


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def minio_test_prefix() -> str:
    """返回仅属于当前集成测试的对象前缀。"""
    return f"integration/{uuid4().hex}"


@pytest.fixture
def minio_store(minio_test_prefix: str) -> Iterator[MinioObjectStorage]:
    """仅在显式启用集成测试时提供真实 MinIO 适配器。"""
    run_integration = os.getenv("RUN_INTEGRATION")
    if run_integration is None:
        pytest.skip("set RUN_INTEGRATION=1 to run MinIO integration tests")
    if run_integration != "1":
        pytest.fail("RUN_INTEGRATION must be exactly '1' when it is set")

    endpoint = os.getenv("TEST_MINIO_ENDPOINT", "localhost:9000")
    access_key = os.getenv("TEST_MINIO_ACCESS_KEY", "minioadmin")
    secret_key = os.getenv("TEST_MINIO_SECRET_KEY", "minioadmin")
    bucket = os.getenv("TEST_MINIO_BUCKET", "graph-rag-integration-tests")
    secure = os.getenv("TEST_MINIO_SECURE", "false").lower() == "true"
    minio_client = Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )
    storage = MinioObjectStorage(client=minio_client, bucket=bucket)

    try:
        yield storage
    finally:
        try:
            for object_info in minio_client.list_objects(
                bucket,
                prefix=f"{minio_test_prefix}/",
                recursive=True,
            ):
                minio_client.remove_object(bucket, object_info.object_name)
        except S3Error as exc:
            if exc.code != "NoSuchBucket":
                raise
        except (InvalidResponseError, HTTPError, OSError, TimeoutError, ConnectionError):
            # 连接失败可能导致清理时连对象前缀都无法列出，
            # 此时不要用清理异常掩盖测试中原本明确的存储异常。
            pass


@pytest.fixture
def real_milvus_store() -> MilvusVectorStore:
    """仅在显式启用集成测试时提供本机 Milvus 适配器。"""
    run_integration = os.getenv("RUN_INTEGRATION")
    if run_integration is None:
        pytest.skip("set RUN_INTEGRATION=1 to run Milvus integration tests")
    if run_integration != "1":
        pytest.fail("RUN_INTEGRATION must be exactly '1' when it is set")

    config = settings.vector_store.model_copy(
        update={
            "enabled": True,
            "host": "localhost",
            "port": 19530,
            "database": "default",
            "uri": "",
            "user": "",
            "password": "",
            "token": "",
            "extra_params": {},
            "connect_timeout": 10,
            "batch_size": 10,
            "consistency_poll_attempts": 20,
            "consistency_poll_interval_seconds": 0.25,
        }
    )
    return MilvusVectorStore(config=config)
