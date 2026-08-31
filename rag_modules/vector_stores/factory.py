from __future__ import annotations

from functools import lru_cache

from rag_modules.config.settings import VectorStoreType, settings
from rag_modules.vector_stores.base import VectorStoreProvider
from rag_modules.vector_stores.milvus import MilvusVectorStore
from rag_modules.vector_stores.stub import StubVectorStore

_SUPPORTED = {"milvus", "pgvector", "qdrant", "weaviate", "opensearch", "elasticsearch"}


@lru_cache(maxsize=8)
def get_vector_store(provider: VectorStoreType | str | None = None) -> VectorStoreProvider:
    selected = provider or settings.vector_store.provider
    if selected not in _SUPPORTED:
        raise ValueError(f"Unsupported vector store: {selected}. Supported: {sorted(_SUPPORTED)}")
    if selected == "milvus":
        return MilvusVectorStore()
    return StubVectorStore(selected)


def get_vector_factory_class(vector_type: str):
    return type(get_vector_store(vector_type))
