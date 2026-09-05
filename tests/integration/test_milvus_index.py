from uuid import uuid4

import pytest
from pymilvus.exceptions import MilvusException

from rag_modules.vector_stores.base import VectorEntity
from rag_modules.vector_stores.milvus import MilvusVectorStore


def _entity(identifier: str, embedding: tuple[float, float, float]) -> VectorEntity:
    return VectorEntity(
        id=identifier,
        embedding=embedding,
        dataset_id=uuid4().hex,
        document_id=uuid4().hex,
        dataset_index_id=uuid4().hex,
        parent_id=None,
        position=0,
    )


def _collection_exists(store: MilvusVectorStore, collection_name: str) -> bool:
    try:
        return store._client().has_collection(
            collection_name=collection_name,
            timeout=10,
        )
    except MilvusException:
        raise AssertionError("Milvus cleanup verification failed.") from None


def test_collection_cleanup_verification_sanitizes_milvus_failure():
    """测试专属资源的清理检查失败时，不得暴露后端细节。"""
    distinctive_backend_detail = "distinctive-backend-detail"

    class FailingClient:
        def has_collection(self, **kwargs):
            raise MilvusException(message=distinctive_backend_detail)

    class StoreWithFailingClient:
        def _client(self):
            return FailingClient()

    with pytest.raises(AssertionError) as error:
        _collection_exists(StoreWithFailingClient(), f"test_{uuid4().hex}")

    assert str(error.value) == "Milvus cleanup verification failed."
    assert distinctive_backend_detail not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


@pytest.mark.integration
def test_milvus_collection_upsert_count_delete_and_drop(real_milvus_store: MilvusVectorStore):
    """显式结构、索引或按主键更新插入的行为发生变化时，本测试必须失败。"""
    collection_name = f"test_{uuid4().hex}"
    segment_id = uuid4().hex

    try:
        real_milvus_store.ensure_collection(collection_name, 3, "COSINE")
        # 第二次调用通过适配器读取服务端的实际结构和索引，
        # 因此会拒绝非显式结构或不采用 HNSW/COSINE 的索引。
        real_milvus_store.ensure_collection(collection_name, 3, "COSINE")
        assert real_milvus_store.upsert(
            collection_name,
            [_entity(segment_id, (0.1, 0.2, 0.3))],
        ) == 1
        assert real_milvus_store.upsert(
            collection_name,
            [_entity(segment_id, (0.2, 0.3, 0.4))],
        ) == 1
        assert real_milvus_store.count(collection_name) == 1
        assert real_milvus_store.delete_ids(collection_name, [segment_id]) == 1
        assert real_milvus_store.count(collection_name) == 0
    finally:
        real_milvus_store.drop_collection(collection_name)

    assert not _collection_exists(real_milvus_store, collection_name)


@pytest.mark.integration
def test_milvus_cleanup_drops_only_its_collection_after_assertion_failure(
    real_milvus_store: MilvusVectorStore,
):
    """即使测试主体抛出异常，也必须删除测试专属集合。"""
    collection_name = f"test_{uuid4().hex}"

    with pytest.raises(AssertionError, match="intentional cleanup exercise"):
        try:
            real_milvus_store.ensure_collection(collection_name, 3, "COSINE")
            assert False, "intentional cleanup exercise"
        finally:
            real_milvus_store.drop_collection(collection_name)

    assert not _collection_exists(real_milvus_store, collection_name)
