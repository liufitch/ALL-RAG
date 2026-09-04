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


@lru_cache(maxsize=8)
def get_vector_store(provider: VectorStoreType | str | None = None) -> VectorStoreProvider:
    selected = provider or settings.vector_store.provider
    if not isinstance(selected, str) or selected not in _SUPPORTED:
        raise VectorValidationError(
            code="VECTOR_PROVIDER_INVALID",
            safe_message="Vector store provider is invalid.",
        )
    if selected == "milvus":
        return MilvusVectorStore()
    raise VectorProviderNotImplemented()


def get_vector_factory_class(vector_type: str):
    return type(get_vector_store(vector_type))
