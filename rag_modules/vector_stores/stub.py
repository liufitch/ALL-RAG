from __future__ import annotations

from rag_modules.vector_stores.base import VectorStoreProvisionResult


class StubVectorStore:
    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name

    def provision_collection(self, collection_name: str, dimension: int, metric_type: str) -> VectorStoreProvisionResult:
        return VectorStoreProvisionResult(
            provider=self.provider_name,
            collection_name=collection_name,
            message=f"{self.provider_name} provider stubbed; implement concrete adapter before production use",
        )

    def drop_collection(self, collection_name: str) -> None:
        return None
