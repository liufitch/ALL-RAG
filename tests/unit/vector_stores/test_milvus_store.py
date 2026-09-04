from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pymilvus import DataType
from pymilvus.exceptions import MilvusException

from rag_modules.config.settings import VectorStoreSettings
from rag_modules.vector_stores.base import (
    VectorConsistencyError,
    VectorEntity,
    VectorProviderNotImplemented,
    VectorSchemaMismatch,
    VectorStoreDisabled,
    VectorStoreError,
    VectorValidationError,
)
from rag_modules.vector_stores.factory import get_vector_store
from rag_modules.vector_stores.milvus import MilvusVectorStore


FIELD_NAMES = {
    "id",
    "embedding",
    "dataset_id",
    "document_id",
    "dataset_index_id",
    "parent_id",
    "position",
}
HUGE_DECIMAL = "9" * 5_000


class RecordingSchema:
    def __init__(self, *, auto_id: bool, enable_dynamic_field: bool) -> None:
        self.auto_id = auto_id
        self.enable_dynamic_field = enable_dynamic_field
        self.fields: dict[str, SimpleNamespace] = {}

    def add_field(self, *, field_name: str, datatype, **kwargs) -> None:
        self.fields[field_name] = SimpleNamespace(
            name=field_name,
            datatype=datatype,
            **kwargs,
        )


class RecordingIndexParams:
    def __init__(self) -> None:
        self.indexes: list[SimpleNamespace] = []

    def add_index(self, *, field_name: str, index_type: str, metric_type: str, params: dict) -> None:
        self.indexes.append(
            SimpleNamespace(
                field_name=field_name,
                index_type=index_type,
                metric_type=metric_type,
                params=params,
            )
        )


def collection_description(dimension: int = 3, *, metric_shape: str = "flat") -> dict:
    fields = [
        {"name": "id", "type": int(DataType.VARCHAR), "params": {"max_length": "36"}, "is_primary": True, "nullable": False},
        {"name": "embedding", "type": int(DataType.FLOAT_VECTOR), "params": {"dim": str(dimension)}, "nullable": False},
        {"name": "dataset_id", "type": int(DataType.VARCHAR), "params": {"max_length": 36}, "nullable": False},
        {"name": "document_id", "type": int(DataType.VARCHAR), "params": {"max_length": 36}, "nullable": False},
        {"name": "dataset_index_id", "type": int(DataType.VARCHAR), "params": {"max_length": 36}, "nullable": False},
        {"name": "parent_id", "type": int(DataType.VARCHAR), "params": {"max_length": 36}, "nullable": True},
        {"name": "position", "type": int(DataType.INT64), "params": {}, "nullable": False},
    ]
    return {
        "collection_name": "collection",
        "auto_id": False,
        "enable_dynamic_field": False,
        "fields": fields,
        "metric_shape": metric_shape,
    }


def object_collection_description(dimension: int = 3) -> SimpleNamespace:
    raw = collection_description(dimension)
    fields = [
        SimpleNamespace(
            name=field["name"],
            datatype=DataType(field["type"]),
            params=SimpleNamespace(**field["params"]),
            is_primary=field.get("is_primary", False),
            nullable=field["nullable"],
        )
        for field in raw["fields"]
    ]
    return SimpleNamespace(auto_id=False, enable_dynamic_field=False, fields=fields)


class RecordingMilvusClient:
    def __init__(
        self,
        *,
        existing: bool = False,
        description=None,
        stats: list[object] | None = None,
        query_counts: list[object] | None = None,
        nested_index: bool = False,
    ) -> None:
        self.existing = existing
        self.description = description or collection_description()
        self.stats = list(stats or [{"row_count": 0}, {"row_count": 0}])
        self.query_counts = list(query_counts or [{"count(*)": 0}, {"count(*)": 0}])
        self.nested_index = nested_index
        self.schema: RecordingSchema | None = None
        self.index_params: RecordingIndexParams | None = None
        self.loaded: list[str] = []
        self.upserted: list[list[dict]] = []
        self.deleted_ids: list[list[str]] = []
        self.deleted_filters: list[str] = []
        self.flushes: list[str] = []
        self.dropped: list[str] = []
        self.has_calls = 0
        self.describe_calls = 0
        self.stats_calls = 0
        self.query_calls: list[dict] = []
        self.fail_with: MilvusException | None = None

    def create_schema(self, *, auto_id: bool, enable_dynamic_field: bool) -> RecordingSchema:
        return RecordingSchema(auto_id=auto_id, enable_dynamic_field=enable_dynamic_field)

    def prepare_index_params(self) -> RecordingIndexParams:
        return RecordingIndexParams()

    def has_collection(self, *, collection_name: str, timeout: int) -> bool:
        self.has_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.existing

    def create_collection(self, *, collection_name: str, schema, index_params, timeout: int) -> None:
        self.existing = True
        self.schema = schema
        self.index_params = index_params

    def describe_collection(self, *, collection_name: str, timeout: int):
        self.describe_calls += 1
        return self.description

    def list_indexes(self, *, collection_name: str, timeout: int) -> list[str]:
        return ["embedding"]

    def describe_index(self, *, collection_name: str, index_name: str, timeout: int):
        if self.nested_index:
            return {
                "field_name": "embedding",
                "index_param": {
                    "index_type": "HNSW",
                    "metric_type": "COSINE",
                    "params": {"M": "16", "efConstruction": "200"},
                },
            }
        return {
            "field_name": "embedding",
            "index_type": "HNSW",
            "metric_type": "COSINE",
            "M": "16",
            "efConstruction": "200",
        }

    def load_collection(self, *, collection_name: str, timeout: int) -> None:
        self.loaded.append(collection_name)

    def upsert(self, *, collection_name: str, data: list[dict], timeout: int):
        self.upserted.append(data)
        return {"upsert_count": len(data)}

    def delete(self, *, collection_name: str, timeout: int, ids=None, filter=None):
        if ids is not None:
            self.deleted_ids.append(ids)
            return {"delete_count": len(ids)}
        self.deleted_filters.append(filter)
        return {"delete_count": 3}

    def flush(self, *, collection_name: str, timeout: int) -> None:
        self.flushes.append(collection_name)

    def get_collection_stats(self, *, collection_name: str, timeout: int):
        self.stats_calls += 1
        return self.stats.pop(0)

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return [self.query_counts.pop(0)]

    def drop_collection(self, *, collection_name: str, timeout: int) -> None:
        self.dropped.append(collection_name)
        self.existing = False


def vector_entity(segment_id: str = "segment-1", embedding=(0.1, 0.2, 0.3)) -> dict:
    return {
        "id": segment_id,
        "embedding": embedding,
        "dataset_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "document_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "dataset_index_id": "cccccccccccccccccccccccccccccccc",
        "parent_id": None,
        "position": 0,
    }


def make_store(client: RecordingMilvusClient, **settings_overrides) -> MilvusVectorStore:
    config = VectorStoreSettings(**settings_overrides)
    return MilvusVectorStore(config=config, client_factory=lambda: client, sleep=lambda _: None)


def test_vector_entity_is_strict_frozen_and_rejects_extra_fields():
    entity = VectorEntity.model_validate(vector_entity())

    with pytest.raises(ValidationError):
        entity.position = 2
    with pytest.raises(ValidationError):
        VectorEntity.model_validate({**vector_entity(), "content": "private"})
    with pytest.raises(ValidationError):
        VectorEntity.model_validate({**vector_entity(), "position": True})
    with pytest.raises(ValidationError):
        VectorEntity.model_validate({**vector_entity(), "embedding": (0.1, "0.2", 0.3)})
    with pytest.raises(ValidationError):
        VectorEntity.model_validate({**vector_entity(), "id": ""})
    with pytest.raises(ValidationError):
        VectorEntity.model_validate({**vector_entity(), "dataset_id": "x" * 37})


def test_ensure_collection_builds_exact_schema_hnsw_index_and_loads():
    client = RecordingMilvusClient()

    make_store(client).ensure_collection("graph_rag_dataset_index", 3, "cosine")

    assert client.schema is not None
    assert set(client.schema.fields) == FIELD_NAMES
    assert client.schema.auto_id is False
    assert client.schema.enable_dynamic_field is False
    assert client.schema.fields["id"].datatype == DataType.VARCHAR
    assert client.schema.fields["id"].max_length == 36
    assert client.schema.fields["id"].is_primary is True
    assert client.schema.fields["embedding"].datatype == DataType.FLOAT_VECTOR
    assert client.schema.fields["embedding"].dim == 3
    assert client.schema.fields["parent_id"].nullable is True
    assert client.schema.fields["position"].datatype == DataType.INT64
    assert client.index_params is not None
    index = client.index_params.indexes[0]
    assert (index.field_name, index.index_type, index.metric_type) == ("embedding", "HNSW", "COSINE")
    assert index.params == {"M": 16, "efConstruction": 200}
    assert client.loaded == ["graph_rag_dataset_index"]


@pytest.mark.parametrize(
    ("description", "nested_index"),
    ((collection_description(), False), (object_collection_description(), True)),
)
def test_existing_collection_accepts_documented_pymilvus_shapes_and_revalidates(description, nested_index):
    client = RecordingMilvusClient(existing=True, description=description, nested_index=nested_index)
    store = make_store(client)

    store.ensure_collection("collection", 3)
    store.ensure_collection("collection", 3)

    assert client.describe_calls == 2
    assert client.loaded == ["collection", "collection"]


def test_existing_collection_accepts_server_metadata_omitting_non_nullable_defaults():
    """PyMilvus 3.0.1 omits nullable=False returned by Milvus 2.5.14."""
    description = collection_description()
    for field in description["fields"]:
        if field["name"] != "parent_id":
            field.pop("nullable")
    client = RecordingMilvusClient(existing=True, description=description)

    make_store(client).ensure_collection("collection", 3)

    assert client.loaded == ["collection"]


@pytest.mark.parametrize(
    "mutate",
    (
        lambda description: description["fields"][1]["params"].update(dim="4"),
        lambda description: description.update(enable_dynamic_field=True),
        lambda description: description["fields"].pop(),
        lambda description: description["fields"][0]["params"].clear(),
        lambda description: description["fields"][0].update(is_primary=False),
        lambda description: description["fields"][6].update(type=int(DataType.VARCHAR)),
        lambda description: description["fields"][5].pop("nullable"),
    ),
)
def test_existing_collection_rejects_wrong_or_ambiguous_schema(mutate):
    description = collection_description()
    mutate(description)
    client = RecordingMilvusClient(existing=True, description=description)
    private_collection_name = "private_collection_name"

    with pytest.raises(VectorSchemaMismatch) as error:
        make_store(client).ensure_collection(private_collection_name, 3)

    assert error.value.code == "VECTOR_SCHEMA_MISMATCH"
    assert private_collection_name not in str(error.value)


def test_existing_collection_rejects_wrong_metric_without_loading():
    client = RecordingMilvusClient(existing=True)
    client.describe_index = lambda **_: {
        "field_name": "embedding",
        "index_type": "HNSW",
        "metric_type": "L2",
        "M": 16,
        "efConstruction": 200,
    }

    with pytest.raises(VectorSchemaMismatch):
        make_store(client).ensure_collection("collection", 3)

    assert client.loaded == []


@pytest.mark.parametrize("bad_name", ("", "1collection", "bad-name", "a" * 256))
def test_collection_name_validation_happens_before_client_creation(bad_name):
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return RecordingMilvusClient()

    store = MilvusVectorStore(config=VectorStoreSettings(), client_factory=factory)

    with pytest.raises(VectorValidationError):
        store.ensure_collection(bad_name, 3)

    assert calls == 0


@pytest.mark.parametrize("dimension", (True, 0, 32769))
def test_dimension_and_metric_validation_happen_before_client_creation(dimension):
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return RecordingMilvusClient()

    store = MilvusVectorStore(config=VectorStoreSettings(), client_factory=factory)
    with pytest.raises(VectorValidationError):
        store.ensure_collection("collection", dimension)
    with pytest.raises(VectorValidationError):
        store.ensure_collection("collection", 3, "L2")
    assert calls == 0


def test_upsert_strictly_validates_all_mappings_before_schema_io():
    client = RecordingMilvusClient(existing=True)
    store = make_store(client)

    with pytest.raises(VectorValidationError):
        store.upsert("collection", [vector_entity(), {**vector_entity("segment-2"), "content": "private"}])

    assert client.has_calls == 0
    assert client.upserted == []


def test_upsert_rejects_position_above_signed_int64_before_client_creation():
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return RecordingMilvusClient(existing=True)

    store = MilvusVectorStore(config=VectorStoreSettings(), client_factory=factory)

    with pytest.raises(VectorValidationError):
        store.upsert("collection", [{**vector_entity(), "position": 2**63}])

    assert calls == 0


def test_empty_upsert_and_delete_return_zero_without_creating_client():
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return RecordingMilvusClient(existing=True)

    store = MilvusVectorStore(config=VectorStoreSettings(), client_factory=factory)

    assert store.upsert("collection", []) == 0
    assert store.delete_ids("collection", []) == 0
    assert calls == 0


def test_upsert_introspects_actual_dimension_after_restart_and_chunks_exactly():
    client = RecordingMilvusClient(existing=True, description=collection_description(3))
    store = make_store(client, batch_size=2)

    count = store.upsert(
        "collection",
        [vector_entity("segment-1"), vector_entity("segment-2"), vector_entity("segment-3")],
    )

    assert count == 3
    assert client.describe_calls == 1
    assert [len(batch) for batch in client.upserted] == [2, 1]
    assert client.upserted[0][0]["id"] == "segment-1"
    assert set(client.upserted[0][0]) == FIELD_NAMES


def test_upsert_rejects_vector_dimension_from_existing_schema_before_write():
    client = RecordingMilvusClient(existing=True, description=collection_description(2))

    with pytest.raises(VectorValidationError):
        make_store(client).upsert("collection", [vector_entity()])

    assert client.describe_calls == 1
    assert client.upserted == []


def test_upsert_rejects_non_finite_vectors_without_schema_io():
    client = RecordingMilvusClient(existing=True)

    with pytest.raises(VectorValidationError):
        make_store(client).upsert("collection", [vector_entity(embedding=(0.1, float("nan"), 0.3))])

    assert client.has_calls == 0


def test_upsert_rejects_inexact_sdk_count_safely():
    client = RecordingMilvusClient(existing=True)
    client.upsert = lambda **_: {"upsert_count": 0}

    with pytest.raises(VectorStoreError) as error:
        make_store(client).upsert("collection", [vector_entity()])

    assert error.value.code == "VECTOR_WRITE_COUNT_MISMATCH"


def test_store_reuses_one_client_and_caches_introspected_schema_for_upserts():
    client = RecordingMilvusClient(existing=True)
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return client

    store = MilvusVectorStore(config=VectorStoreSettings(), client_factory=factory)
    store.upsert("collection", [vector_entity()])
    store.upsert("collection", [vector_entity("segment-2")])

    assert factory_calls == 1
    assert client.describe_calls == 1


def test_delete_ids_deduplicates_chunks_and_returns_exact_count():
    client = RecordingMilvusClient(existing=True)

    deleted = make_store(client, batch_size=2).delete_ids(
        "collection", ["segment-1", "segment-2", "segment-1", "segment-3"]
    )

    assert deleted == 3
    assert client.deleted_ids == [["segment-1", "segment-2"], ["segment-3"]]


def test_delete_ids_validates_every_id_before_client_creation():
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return RecordingMilvusClient(existing=True)

    store = MilvusVectorStore(config=VectorStoreSettings(), client_factory=factory)

    with pytest.raises(VectorValidationError):
        store.delete_ids("collection", ["segment-1", ""])

    assert calls == 0


@pytest.mark.parametrize(
    "document_id",
    (
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ),
)
def test_delete_document_requires_uuid_and_uses_escaped_equality_filter(document_id):
    client = RecordingMilvusClient(existing=True)
    store = make_store(client)

    with pytest.raises(VectorValidationError):
        store.delete_document("collection", 'bad"value')
    deleted = store.delete_document("collection", document_id)

    assert deleted == 3
    assert client.deleted_filters == [f'document_id == "{document_id}"']


def test_count_flushes_and_returns_only_after_two_equal_logical_count_observations():
    client = RecordingMilvusClient(
        existing=True,
        query_counts=[{"count(*)": "2"}, {"count(*)": 3}, {"count(*)": "3"}],
    )
    sleeps: list[float] = []
    store = MilvusVectorStore(
        config=VectorStoreSettings(consistency_poll_attempts=5, consistency_poll_interval_seconds=0.25),
        client_factory=lambda: client,
        sleep=sleeps.append,
    )

    assert store.count("collection") == 3
    assert client.flushes == ["collection"]
    assert client.query_calls == [
        {
            "collection_name": "collection",
            "filter": "",
            "output_fields": ["count(*)"],
            "timeout": 5,
        }
    ] * 3
    assert sleeps == [0.25, 0.25]


def test_count_raises_retryable_safe_error_when_observations_never_stabilize():
    client = RecordingMilvusClient(
        existing=True,
        query_counts=[{"count(*)": 1}, {"count(*)": 2}, {"count(*)": 3}],
    )
    store = make_store(client, consistency_poll_attempts=3)
    private_collection_name = "private_collection_name"

    with pytest.raises(VectorConsistencyError) as error:
        store.count(private_collection_name)

    assert error.value.retryable is True
    assert error.value.code == "VECTOR_COUNT_UNSTABLE"
    assert private_collection_name not in str(error.value)


def test_count_treats_invalid_observations_as_unstable_and_never_uses_last_value():
    client = RecordingMilvusClient(
        existing=True,
        query_counts=[{"count(*)": True}, {}, {"count(*)": -1}],
    )
    sleeps: list[float] = []
    store = MilvusVectorStore(
        config=VectorStoreSettings(
            consistency_poll_attempts=3,
            consistency_poll_interval_seconds=0.25,
        ),
        client_factory=lambda: client,
        sleep=sleeps.append,
    )

    with pytest.raises(VectorConsistencyError):
        store.count("collection")

    assert len(client.query_calls) == 3
    assert client.flushes == ["collection"]
    assert sleeps == [0.25, 0.25]


def test_count_treats_huge_decimal_observations_as_unstable_with_bounded_polling():
    client = RecordingMilvusClient(
        existing=True,
        query_counts=[{"count(*)": HUGE_DECIMAL}] * 3,
    )
    sleeps: list[float] = []
    store = MilvusVectorStore(
        config=VectorStoreSettings(
            consistency_poll_attempts=3,
            consistency_poll_interval_seconds=0.25,
        ),
        client_factory=lambda: client,
        sleep=sleeps.append,
    )

    with pytest.raises(VectorConsistencyError):
        store.count("collection")

    assert len(client.query_calls) == 3
    assert client.flushes == ["collection"]
    assert sleeps == [0.25, 0.25]


def test_existing_collection_rejects_huge_decimal_schema_numeric_metadata():
    description = collection_description()
    description["fields"][1]["params"]["dim"] = HUGE_DECIMAL
    client = RecordingMilvusClient(existing=True, description=description)

    with pytest.raises(VectorSchemaMismatch):
        make_store(client).ensure_collection("collection", 3)

    assert client.loaded == []


def test_existing_collection_rejects_huge_decimal_index_numeric_metadata():
    client = RecordingMilvusClient(existing=True)
    client.describe_index = lambda **_: {
        "field_name": "embedding",
        "index_type": "HNSW",
        "metric_type": "COSINE",
        "M": HUGE_DECIMAL,
        "efConstruction": "200",
    }

    with pytest.raises(VectorSchemaMismatch):
        make_store(client).ensure_collection("collection", 3)

    assert client.loaded == []


def test_existing_collection_rejects_conflicting_nested_and_direct_index_metadata():
    client = RecordingMilvusClient(existing=True)
    client.describe_index = lambda **_: {
        "field_name": "embedding",
        "index_param": {
            "index_type": "HNSW",
            "metric_type": "COSINE",
            "params": {"M": "16", "efConstruction": "200"},
        },
        "index_type": "IVF_FLAT",
        "metric_type": "L2",
        "M": "32",
        "efConstruction": "400",
    }

    with pytest.raises(VectorSchemaMismatch):
        make_store(client).ensure_collection("collection", 3)

    assert client.loaded == []


def test_drop_is_idempotent_and_invalidates_cached_schema():
    client = RecordingMilvusClient(existing=True)
    store = make_store(client)
    store.upsert("collection", [vector_entity()])

    store.drop_collection("collection")
    store.drop_collection("collection")
    client.existing = True
    store.upsert("collection", [vector_entity("segment-2")])

    assert client.dropped == ["collection"]
    assert client.describe_calls == 2


def test_disabled_provision_remains_compatible_but_indexing_fails_before_client_creation():
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return RecordingMilvusClient()

    store = MilvusVectorStore(config=VectorStoreSettings(enabled=False), client_factory=factory)

    result = store.provision_collection("collection", 3, "COSINE")
    with pytest.raises(VectorStoreDisabled):
        store.ensure_collection("collection", 3)
    with pytest.raises(VectorStoreDisabled):
        store.upsert("collection", [vector_entity()])
    with pytest.raises(VectorStoreDisabled):
        store.count("collection")
    with pytest.raises(VectorStoreDisabled):
        store.delete_ids("collection", ["segment-1"])
    with pytest.raises(VectorStoreDisabled):
        store.delete_document("collection", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    with pytest.raises(VectorStoreDisabled):
        store.drop_collection("collection")

    assert result.message == "milvus disabled, skip provisioning"
    assert calls == 0


def test_known_unimplemented_factory_provider_fails_fast():
    get_vector_store.cache_clear()
    with pytest.raises(VectorProviderNotImplemented):
        get_vector_store("pgvector")


def test_unknown_factory_provider_is_a_safe_configuration_error():
    get_vector_store.cache_clear()
    with pytest.raises(VectorValidationError) as error:
        get_vector_store("private-provider")

    assert error.value.code == "VECTOR_PROVIDER_INVALID"
    assert "private-provider" not in str(error.value)


@pytest.mark.parametrize("provider", ("", 0, False))
def test_explicit_falsy_provider_is_rejected_before_provider_construction(
    monkeypatch, provider
):
    def fail_if_constructed():
        raise AssertionError("provider constructed")

    monkeypatch.setattr(
        "rag_modules.vector_stores.factory.MilvusVectorStore",
        fail_if_constructed,
    )
    get_vector_store.cache_clear()

    with pytest.raises(VectorValidationError) as error:
        get_vector_store(provider)

    assert error.value.code == "VECTOR_PROVIDER_INVALID"


@pytest.mark.parametrize("provider", ([], {}))
def test_explicit_unhashable_provider_is_a_safe_configuration_error(provider):
    get_vector_store.cache_clear()

    with pytest.raises(VectorValidationError) as error:
        get_vector_store(provider)

    assert error.value.code == "VECTOR_PROVIDER_INVALID"


def test_factory_caches_configured_default_and_same_valid_provider_identity(monkeypatch):
    monkeypatch.setattr(
        "rag_modules.vector_stores.factory.settings.vector_store.provider",
        "milvus",
    )
    get_vector_store.cache_clear()

    explicit = get_vector_store("milvus")

    assert get_vector_store("milvus") is explicit
    assert get_vector_store() is explicit
    assert get_vector_store(None) is explicit


def test_known_milvus_exception_is_wrapped_without_sensitive_details():
    client = RecordingMilvusClient()
    client.fail_with = MilvusException(message="secret backend vector and identifier")

    with pytest.raises(VectorStoreError) as error:
        make_store(client).ensure_collection("collection", 3)

    assert error.value.code == "VECTOR_STORE_OPERATION_FAILED"
    assert error.value.retryable is True
    assert "secret" not in str(error.value)
    assert "collection" not in str(error.value)


def test_unrelated_programmer_error_is_not_swallowed():
    client = RecordingMilvusClient()
    client.has_collection = lambda **_: (_ for _ in ()).throw(RuntimeError("programmer defect"))

    with pytest.raises(RuntimeError, match="programmer defect"):
        make_store(client).ensure_collection("collection", 3)
