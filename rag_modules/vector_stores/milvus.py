from __future__ import annotations

from pymilvus import MilvusClient

from rag_modules.config.settings import settings
from rag_modules.vector_stores.base import VectorStoreProvisionResult


class MilvusVectorStore:
    provider_name = "milvus"

    def __init__(self) -> None:
        self._config = settings.vector_store

    def _client(self) -> MilvusClient:
        token = self._config.token or (
            f"{self._config.user}:{self._config.password}"
            if self._config.user and self._config.password
            else None
        )
        uri = self._config.uri or f"http://{self._config.host}:{self._config.port}"
        kwargs = {"uri": uri}
        if token:
            kwargs["token"] = token
        client = MilvusClient(**kwargs)
        if self._config.database != "default" and hasattr(client, "use_database"):
            client.use_database(db_name=self._config.database)
        return client

    def provision_collection(self, collection_name: str, dimension: int, metric_type: str) -> VectorStoreProvisionResult:
        if not self._config.enabled:
            return VectorStoreProvisionResult(
                provider=self.provider_name,
                collection_name=collection_name,
                message="milvus disabled, skip provisioning",
            )

        client = self._client()
        collections = client.list_collections(timeout=self._config.connect_timeout)
        if collection_name not in collections:
            client.create_collection(
                collection_name=collection_name,
                dimension=dimension,
                primary_field_name="id",
                id_type="string",
                vector_field_name="embedding",
                metric_type=metric_type,
                auto_id=False,
                max_length=128,
                timeout=self._config.connect_timeout,
            )
        return VectorStoreProvisionResult(
            provider=self.provider_name,
            collection_name=collection_name,
            message="milvus collection ready",
        )

    def drop_collection(self, collection_name: str) -> None:
        if not self._config.enabled:
            return
        client = self._client()
        collections = client.list_collections(timeout=self._config.connect_timeout)
        if collection_name in collections:
            client.drop_collection(
                collection_name=collection_name,
                timeout=self._config.connect_timeout,
            )
