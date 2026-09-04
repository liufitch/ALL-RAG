from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import ValidationError
from pymilvus import DataType, MilvusClient
from pymilvus.exceptions import MilvusException

from rag_modules.config.settings import VectorStoreSettings, settings
from rag_modules.vector_stores.base import (
    VectorConsistencyError,
    VectorEntity,
    VectorSchemaMismatch,
    VectorStoreDisabled,
    VectorStoreError,
    VectorStoreProvisionResult,
    VectorValidationError,
)


_COLLECTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,254}\Z")
_UUID = re.compile(
    r"(?:[0-9A-Fa-f]{32}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\Z"
)
_EXPECTED_FIELDS = {
    "id": (DataType.VARCHAR, False, True, 36),
    "embedding": (DataType.FLOAT_VECTOR, False, False, None),
    "dataset_id": (DataType.VARCHAR, False, False, 36),
    "document_id": (DataType.VARCHAR, False, False, 36),
    "dataset_index_id": (DataType.VARCHAR, False, False, 36),
    "parent_id": (DataType.VARCHAR, True, False, 36),
    "position": (DataType.INT64, False, False, None),
}
_INT64_MAX = 9_223_372_036_854_775_807
_INT64_DECIMAL_DIGITS = 19
_MISSING = object()


class MilvusVectorStore:
    provider_name = "milvus"

    def __init__(
        self,
        config: VectorStoreSettings | None = None,
        *,
        client_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config or settings.vector_store
        self._client_factory = client_factory or self._create_client
        self._client_instance: Any | None = None
        self._schema_dimensions: dict[str, int] = {}
        self._sleep = sleep

    def provision_collection(
        self, collection_name: str, dimension: int, metric_type: str
    ) -> VectorStoreProvisionResult:
        if not self._config.enabled:
            return VectorStoreProvisionResult(
                provider=self.provider_name,
                collection_name=collection_name,
                message="milvus disabled, skip provisioning",
            )
        self.ensure_collection(collection_name, dimension, metric_type)
        return VectorStoreProvisionResult(
            provider=self.provider_name,
            collection_name=collection_name,
            message="milvus collection ready",
        )

    def ensure_collection(
        self, collection_name: str, dimension: int, metric_type: str = "COSINE"
    ) -> None:
        self._require_enabled()
        _validate_collection_name(collection_name)
        _validate_dimension(dimension)
        metric = _validate_metric(metric_type)
        client = self._client()
        try:
            if client.has_collection(
                collection_name=collection_name,
                timeout=self._config.connect_timeout,
            ):
                actual_dimension = self._inspect_collection(client, collection_name)
                if actual_dimension != dimension:
                    raise VectorSchemaMismatch()
            else:
                self._create_collection(client, collection_name, dimension, metric)
                actual_dimension = dimension
            client.load_collection(
                collection_name=collection_name,
                timeout=self._config.connect_timeout,
            )
        except MilvusException:
            raise _operation_failed() from None
        self._schema_dimensions[collection_name] = actual_dimension

    def upsert(
        self,
        collection_name: str,
        entities: Sequence[VectorEntity | dict[str, Any]],
    ) -> int:
        self._require_enabled()
        _validate_collection_name(collection_name)
        validated = _validate_entities(entities)
        if not validated:
            return 0
        entity_dimension = len(validated[0].embedding)
        if any(len(entity.embedding) != entity_dimension for entity in validated):
            raise VectorValidationError()

        client = self._client()
        try:
            collection_dimension = self._schema_dimensions.get(collection_name)
            if collection_dimension is None:
                collection_dimension = self._inspect_collection(client, collection_name)
                self._schema_dimensions[collection_name] = collection_dimension
            if collection_dimension != entity_dimension:
                raise VectorValidationError()

            written = 0
            for batch in _chunks(validated, self._config.batch_size):
                response = client.upsert(
                    collection_name=collection_name,
                    data=[_entity_data(entity) for entity in batch],
                    timeout=self._config.connect_timeout,
                )
                batch_count = _result_count(response, "upsert_count")
                if batch_count != len(batch):
                    raise VectorStoreError(
                        "VECTOR_WRITE_COUNT_MISMATCH",
                        True,
                        "Vector write count did not match.",
                    )
                written += batch_count
            return written
        except MilvusException:
            raise _operation_failed() from None

    def count(self, collection_name: str) -> int:
        self._require_enabled()
        _validate_collection_name(collection_name)
        client = self._client()
        try:
            client.flush(
                collection_name=collection_name,
                timeout=self._config.connect_timeout,
            )
            previous: int | None = None
            for attempt in range(self._config.consistency_poll_attempts):
                observation = _logical_count(
                    client.query(
                        collection_name=collection_name,
                        filter="",
                        output_fields=["count(*)"],
                        timeout=self._config.connect_timeout,
                    )
                )
                if observation is not None and observation == previous:
                    return observation
                previous = observation
                if attempt + 1 < self._config.consistency_poll_attempts:
                    self._sleep(self._config.consistency_poll_interval_seconds)
        except MilvusException:
            raise _operation_failed() from None
        raise VectorConsistencyError()

    def delete_ids(self, collection_name: str, ids: Sequence[str]) -> int:
        self._require_enabled()
        _validate_collection_name(collection_name)
        validated = _validate_ids(ids)
        if not validated:
            return 0
        client = self._client()
        try:
            deleted = 0
            for batch in _chunks(validated, self._config.batch_size):
                response = client.delete(
                    collection_name=collection_name,
                    ids=list(batch),
                    timeout=self._config.connect_timeout,
                )
                deleted += _result_count(response, "delete_count")
            return deleted
        except MilvusException:
            raise _operation_failed() from None

    def delete_document(self, collection_name: str, document_id: str) -> int:
        self._require_enabled()
        _validate_collection_name(collection_name)
        if not isinstance(document_id, str) or not _UUID.fullmatch(document_id):
            raise VectorValidationError()
        client = self._client()
        try:
            response = client.delete(
                collection_name=collection_name,
                filter=f'document_id == "{document_id}"',
                timeout=self._config.connect_timeout,
            )
            return _result_count(response, "delete_count")
        except MilvusException:
            raise _operation_failed() from None

    def drop_collection(self, collection_name: str) -> None:
        self._require_enabled()
        _validate_collection_name(collection_name)
        client = self._client()
        try:
            if client.has_collection(
                collection_name=collection_name,
                timeout=self._config.connect_timeout,
            ):
                client.drop_collection(
                    collection_name=collection_name,
                    timeout=self._config.connect_timeout,
                )
        except MilvusException:
            raise _operation_failed() from None
        finally:
            self._schema_dimensions.pop(collection_name, None)

    def _client(self) -> Any:
        if self._client_instance is None:
            try:
                self._client_instance = self._client_factory()
            except MilvusException:
                raise _operation_failed() from None
        return self._client_instance

    def _create_client(self) -> MilvusClient:
        uri = self._config.uri or f"http://{self._config.host}:{self._config.port}"
        token = self._config.token or (
            f"{self._config.user}:{self._config.password}"
            if self._config.user and self._config.password
            else ""
        )
        kwargs = dict(self._config.extra_params)
        kwargs.update(
            uri=uri,
            db_name=self._config.database,
            timeout=self._config.connect_timeout,
        )
        if token:
            kwargs["token"] = token
        return MilvusClient(**kwargs)

    def _create_collection(
        self, client: Any, collection_name: str, dimension: int, metric: str
    ) -> None:
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(
            field_name="id",
            datatype=DataType.VARCHAR,
            max_length=36,
            is_primary=True,
            nullable=False,
        )
        schema.add_field(
            field_name="embedding",
            datatype=DataType.FLOAT_VECTOR,
            dim=dimension,
            nullable=False,
        )
        for field_name in ("dataset_id", "document_id", "dataset_index_id"):
            schema.add_field(
                field_name=field_name,
                datatype=DataType.VARCHAR,
                max_length=36,
                nullable=False,
            )
        schema.add_field(
            field_name="parent_id",
            datatype=DataType.VARCHAR,
            max_length=36,
            nullable=True,
        )
        schema.add_field(
            field_name="position",
            datatype=DataType.INT64,
            nullable=False,
        )
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_type="HNSW",
            metric_type=metric,
            params={"M": 16, "efConstruction": 200},
        )
        client.create_collection(
            collection_name=collection_name,
            schema=schema,
            index_params=index_params,
            timeout=self._config.connect_timeout,
        )

    def _inspect_collection(self, client: Any, collection_name: str) -> int:
        description = client.describe_collection(
            collection_name=collection_name,
            timeout=self._config.connect_timeout,
        )
        dimension = _schema_dimension(description)
        indexes = client.list_indexes(
            collection_name=collection_name,
            timeout=self._config.connect_timeout,
        )
        if not isinstance(indexes, list) or len(indexes) != 1 or not isinstance(indexes[0], str):
            raise VectorSchemaMismatch()
        index = client.describe_index(
            collection_name=collection_name,
            index_name=indexes[0],
            timeout=self._config.connect_timeout,
        )
        _validate_index(index)
        return dimension

    def _require_enabled(self) -> None:
        if not self._config.enabled:
            raise VectorStoreDisabled()


def _validate_collection_name(collection_name: str) -> None:
    if not isinstance(collection_name, str) or not _COLLECTION_NAME.fullmatch(collection_name):
        raise VectorValidationError()


def _validate_dimension(dimension: int) -> None:
    if type(dimension) is not int or not 1 <= dimension <= 32_768:
        raise VectorValidationError()


def _validate_metric(metric_type: str) -> str:
    if not isinstance(metric_type, str) or metric_type.upper() != "COSINE":
        raise VectorValidationError()
    return "COSINE"


def _validate_entities(
    entities: Sequence[VectorEntity | dict[str, Any]],
) -> tuple[VectorEntity, ...]:
    if isinstance(entities, (str, bytes)) or not isinstance(entities, Sequence):
        raise VectorValidationError()
    try:
        return tuple(
            entity if isinstance(entity, VectorEntity) else VectorEntity.model_validate(entity)
            for entity in entities
        )
    except (ValidationError, TypeError, ValueError, OverflowError):
        raise VectorValidationError() from None


def _validate_ids(ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(ids, (str, bytes)) or not isinstance(ids, Sequence):
        raise VectorValidationError()
    unique: list[str] = []
    seen: set[str] = set()
    for identifier in ids:
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 36:
            raise VectorValidationError()
        if identifier not in seen:
            seen.add(identifier)
            unique.append(identifier)
    return tuple(unique)


def _chunks(values: Sequence[Any], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _entity_data(entity: VectorEntity) -> dict[str, Any]:
    return {
        "id": entity.id,
        "embedding": list(entity.embedding),
        "dataset_id": entity.dataset_id,
        "document_id": entity.document_id,
        "dataset_index_id": entity.dataset_index_id,
        "parent_id": entity.parent_id,
        "position": entity.position,
    }


def _result_count(response: Any, key: str) -> int:
    value = response.get(key) if isinstance(response, Mapping) else None
    if type(value) is not int or value < 0:
        raise VectorStoreError(
            "VECTOR_STORE_RESPONSE_INVALID",
            True,
            "Vector store response is invalid.",
        )
    return value


def _logical_count(rows: Any) -> int | None:
    if not isinstance(rows, (list, tuple)) or len(rows) != 1:
        return None
    value = rows[0].get("count(*)") if isinstance(rows[0], Mapping) else None
    if type(value) is int:
        return value if value >= 0 else None
    return _bounded_ascii_decimal(value)


def _schema_dimension(description: Any) -> int:
    if _member(description, "auto_id") is not False:
        raise VectorSchemaMismatch()
    if _member(description, "enable_dynamic_field") is not False:
        raise VectorSchemaMismatch()
    fields = _member(description, "fields")
    if not isinstance(fields, (list, tuple)):
        raise VectorSchemaMismatch()
    by_name: dict[str, Any] = {}
    for field in fields:
        name = _member(field, "name")
        if not isinstance(name, str) or name in by_name:
            raise VectorSchemaMismatch()
        by_name[name] = field
    if set(by_name) != set(_EXPECTED_FIELDS):
        raise VectorSchemaMismatch()

    dimension: int | None = None
    for name, (datatype, nullable, primary, max_length) in _EXPECTED_FIELDS.items():
        field = by_name[name]
        if _field_datatype(field) != int(datatype):
            raise VectorSchemaMismatch()
        if _member(field, "nullable", False) is not nullable:
            raise VectorSchemaMismatch()
        is_primary = _member(field, "is_primary", False)
        if is_primary is not primary:
            raise VectorSchemaMismatch()
        params = _member(field, "params")
        if params is _MISSING:
            raise VectorSchemaMismatch()
        if max_length is not None and _positive_int(_member(params, "max_length")) != max_length:
            raise VectorSchemaMismatch()
        if name == "embedding":
            dimension = _positive_int(_member(params, "dim"))
            if dimension is None:
                raise VectorSchemaMismatch()
    if dimension is None:
        raise VectorSchemaMismatch()
    return dimension


def _field_datatype(field: Any) -> int | None:
    values = [
        value
        for name in ("type", "datatype")
        if (value := _member(field, name)) is not _MISSING
    ]
    if len(values) != 1 or isinstance(values[0], bool):
        return None
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return None


def _validate_index(index: Any) -> None:
    if _member(index, "field_name") != "embedding":
        raise VectorSchemaMismatch()
    nested = _member(index, "index_param")
    if nested is _MISSING:
        sources = ((index, False),)
    else:
        direct_fields = ("index_type", "metric_type", "M", "efConstruction")
        has_direct = any(_member(index, name) is not _MISSING for name in direct_fields)
        sources = ((nested, True), (index, False)) if has_direct else ((nested, True),)
    if any(
        _index_signature(source, nested_params) != ("HNSW", "COSINE", 16, 200)
        for source, nested_params in sources
    ):
        raise VectorSchemaMismatch()


def _index_signature(
    source: Any, nested_params: bool
) -> tuple[Any, Any, int | None, int | None]:
    params = _member(source, "params") if nested_params else source
    return (
        _member(source, "index_type"),
        _member(source, "metric_type"),
        _positive_int(_member(params, "M")),
        _positive_int(_member(params, "efConstruction")),
    )


def _member(value: Any, name: str, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _positive_int(value: Any) -> int | None:
    if type(value) is int:
        return value if value > 0 else None
    parsed = _bounded_ascii_decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _bounded_ascii_decimal(value: Any) -> int | None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _INT64_DECIMAL_DIGITS
        or not value.isascii()
        or not value.isdecimal()
    ):
        return None
    parsed = int(value)
    return parsed if parsed <= _INT64_MAX else None


def _operation_failed() -> VectorStoreError:
    return VectorStoreError(
        "VECTOR_STORE_OPERATION_FAILED",
        True,
        "Vector store operation failed.",
    )
