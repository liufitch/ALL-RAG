from __future__ import annotations

from functools import lru_cache

from rag_modules.config.settings import VectorStoreType, settings
from rag_modules.vector_stores.base import (
    VectorProviderNotImplemented,
    VectorStoreProvider,
    VectorValidationError,
)
from rag_modules.vector_stores.milvus import MilvusVectorStore

_SUPPORTED = {"milvus", "pgvector", "qdrant", "weaviate", "opensearch", "elasticsearch"}


def get_vector_store(provider: VectorStoreType | str | None = None) -> VectorStoreProvider:
    selected = settings.vector_store.provider if provider is None else provider
    if not isinstance(selected, str) or selected not in _SUPPORTED:
        raise VectorValidationError(
            code="VECTOR_PROVIDER_INVALID",
            safe_message="Vector store provider is invalid.",
        )
    return _get_vector_store(selected)


@lru_cache(maxsize=8)
def _get_vector_store(provider: str) -> VectorStoreProvider:
    if provider == "milvus":
        return MilvusVectorStore()
    raise VectorProviderNotImplemented()


# 保留原先带缓存装饰器的入口所提供的缓存重置方法。
get_vector_store.cache_clear = _get_vector_store.cache_clear  # type: ignore[attr-defined]


def get_vector_factory_class(vector_type: str):
    return type(get_vector_store(vector_type))
