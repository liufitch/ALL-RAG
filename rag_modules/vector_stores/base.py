from __future__ import annotations

import math
from typing import Any, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VectorStoreError(Exception):
    """Stable sanitized failure raised by vector-store boundaries."""

    def __init__(self, code: str, retryable: bool, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.retryable = retryable
        self.safe_message = safe_message


class VectorSchemaMismatch(VectorStoreError):
    def __init__(self) -> None:
        super().__init__(
            "VECTOR_SCHEMA_MISMATCH",
            False,
            "Vector collection schema does not match.",
        )


class VectorValidationError(VectorStoreError):
    def __init__(
        self,
        code: str = "VECTOR_INPUT_INVALID",
        safe_message: str = "Vector store input is invalid.",
    ) -> None:
        super().__init__(code, False, safe_message)


class VectorStoreDisabled(VectorStoreError):
    def __init__(self) -> None:
        super().__init__(
            "VECTOR_STORE_DISABLED",
            False,
            "Vector store is disabled.",
        )


class VectorProviderNotImplemented(VectorStoreError):
    def __init__(self) -> None:
        super().__init__(
            "VECTOR_PROVIDER_NOT_IMPLEMENTED",
            False,
            "Vector store provider is not implemented.",
        )


class VectorConsistencyError(VectorStoreError):
    def __init__(self) -> None:
        super().__init__(
            "VECTOR_COUNT_UNSTABLE",
            True,
            "Vector collection count did not stabilize.",
        )


class VectorEntity(BaseModel):
    """The exact vector payload shared by indexing and Milvus."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(min_length=1, max_length=36)
    embedding: tuple[float, ...]
    dataset_id: str = Field(min_length=1, max_length=36)
    document_id: str = Field(min_length=1, max_length=36)
    dataset_index_id: str = Field(min_length=1, max_length=36)
    parent_id: str | None = Field(default=None, min_length=1, max_length=36)
    position: int = Field(ge=0, le=9_223_372_036_854_775_807)

    @field_validator("embedding", mode="before")
    @classmethod
    def validate_embedding(cls, value: Any) -> Any:
        if (
            not isinstance(value, tuple)
            or not value
            or any(
                type(component) not in (int, float)
                or not math.isfinite(float(component))
                for component in value
            )
        ):
            raise ValueError("embedding must contain finite numeric values")
        return value


class VectorStoreProvisionResult(BaseModel):
    provider: str
    collection_name: str
    message: str


class VectorStoreProvider(Protocol):
    provider_name: str

    def provision_collection(self, collection_name: str, dimension: int, metric_type: str) -> VectorStoreProvisionResult:
        ...

    def ensure_collection(
        self, collection_name: str, dimension: int, metric_type: str = "COSINE"
    ) -> None:
        ...

    def upsert(
        self, collection_name: str, entities: Sequence[VectorEntity | dict[str, Any]]
    ) -> int:
        ...

    def count(self, collection_name: str) -> int:
        ...

    def delete_ids(self, collection_name: str, ids: Sequence[str]) -> int:
        ...

    def delete_document(self, collection_name: str, document_id: str) -> int:
        ...

    def drop_collection(self, collection_name: str) -> None:
        ...
