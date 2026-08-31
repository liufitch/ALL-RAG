from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class VectorStoreProvisionResult(BaseModel):
    provider: str
    collection_name: str
    message: str


class VectorStoreProvider(Protocol):
    provider_name: str

    def provision_collection(self, collection_name: str, dimension: int, metric_type: str) -> VectorStoreProvisionResult:
        ...

    def drop_collection(self, collection_name: str) -> None:
        ...
