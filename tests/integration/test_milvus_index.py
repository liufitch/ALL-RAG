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
    """A failed test-owned cleanup check must not expose backend details."""
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
    """A changed explicit schema/index or primary-key upsert must fail this test."""
    collection_name = f"test_{uuid4().hex}"
    segment_id = uuid4().hex

    try:
        real_milvus_store.ensure_collection(collection_name, 3, "COSINE")
        # The second call reads the actual server schema and index through the
        # adapter, so a non-explicit schema or non-HNSW/COSINE index is rejected.
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
    """The test-owned collection is removed even when its body raises."""
    collection_name = f"test_{uuid4().hex}"

    with pytest.raises(AssertionError, match="intentional cleanup exercise"):
        try:
            real_milvus_store.ensure_collection(collection_name, 3, "COSINE")
            assert False, "intentional cleanup exercise"
        finally:
            real_milvus_store.drop_collection(collection_name)

    assert not _collection_exists(real_milvus_store, collection_name)
