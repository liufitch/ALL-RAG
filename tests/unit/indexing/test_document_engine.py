from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields, replace
import gc
from io import BytesIO
import threading
from typing import Any
import weakref

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rag_modules.embeddings import EmbeddingBatch
from rag_modules.db.models import DocumentSegmentRecord
from rag_modules.indexing.engine import DocumentIndexingEngine, DocumentIndexingError
from rag_modules.indexing.keywords import KeywordExtractor
from rag_modules.indexing.models import (
    IndexDocumentCommand,
    SegmentStagingCommand,
    VectorTarget,
)
from rag_modules.parsing import (
    DocumentParseError,
    ParsedBlock,
    ParsedDocument,
    ParserWarning,
)
from rag_modules.segmentation import (
    GeneralSegmentationConfig,
    ParentChildSegmentationConfig,
    Segmenter,
)
from rag_modules.repositories.segment_repository import SegmentRepository
from rag_modules.vector_stores.base import VectorStoreError


@dataclass
class FakeRecord:
    id: str
    dataset_id: str
    dataset_index_id: str
    document_id: str
    parent_id: str | None
    position: int
    content: str
    index_type: str
    embedding_status: str
    status: str = "indexing"
    deleted_at: object | None = None
    keywords: list[str] | None = None


class FakeStorage:
    def __init__(self, events: list[str], *, forbidden: bool = False) -> None:
        self.events = events
        self.forbidden = forbidden
        self.stream: BytesIO | None = None
        self.requested_keys: list[str] = []

    @asynccontextmanager
    async def get_stream(self, object_key: str):
        if self.forbidden:
            raise AssertionError("storage must not be called")
        self.events.append("download")
        self.requested_keys.append(object_key)
        self.stream = BytesIO(b"source bytes")
        try:
            yield self.stream
        finally:
            self.stream.close()
            self.events.append("storage-closed")


class FakeParserRegistry:
    def __init__(
        self,
        events: list[str],
        blocks: tuple[ParsedBlock, ...],
        *,
        warnings: tuple[ParserWarning, ...] = (),
        fail: Exception | None = None,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.events = events
        self.blocks = blocks
        self.warnings = warnings
        self.fail = fail
        self.started = started
        self.release = release
        self.thread_ids: list[int] = []
        self.contexts: list[Any] = []

    def parse(self, extension, stream, context):
        self.thread_ids.append(threading.get_ident())
        self.events.append("parse")
        self.contexts.append((extension, context))
        assert not stream.closed
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            assert self.release.wait(timeout=2)
            assert not stream.closed
        stream.read()
        if self.fail:
            raise self.fail
        return ParsedDocument(
            document_id=context.document_id,
            filename=context.filename,
            source_type=extension.lstrip("."),
            blocks=self.blocks,
            metadata={},
            warnings=self.warnings,
        )


class RecordingSegmenter:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.thread_ids: list[int] = []
        self._real = Segmenter()

    def segment(self, parsed, config):
        self.thread_ids.append(threading.get_ident())
        self.events.append("split")
        return self._real.segment(parsed, config)


class FakeSegmentRepository:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.records: list[FakeRecord] = []
        self.keyword_flushes: list[dict[str, tuple[str, ...]]] = []
        self.embedding_flushes: list[tuple[str, ...]] = []

    async def stage(self, command, segments):
        self.events.append("stage")
        parent_ids = {
            segment.local_id: f"rec-{segment.position}"
            for segment in segments
            if segment.index_type == "parent"
        }
        self.records = [
            FakeRecord(
                id=f"rec-{segment.position}",
                dataset_id=command.dataset_id,
                dataset_index_id=command.dataset_index_id,
                document_id=command.document_id,
                parent_id=parent_ids.get(segment.parent_local_id),
                position=segment.position,
                content=segment.content,
                index_type=segment.index_type,
                embedding_status=(
                    "not_required"
                    if command.indexing_technique == "economy"
                    or segment.index_type == "parent"
                    else "waiting"
                ),
            )
            for segment in segments
        ]
        return self.records

    async def update_keywords(
        self, *, dataset_index_id, document_id, keywords_by_segment_id
    ):
        assert dataset_index_id == "index-1"
        assert document_id == "doc-1"
        updates = {
            key: tuple(value) for key, value in keywords_by_segment_id.items()
        }
        self.keyword_flushes.append(updates)
        for record in self.records:
            if record.id in updates:
                record.keywords = list(updates[record.id])

    async def mark_embeddings_completed(
        self, *, dataset_index_id, document_id, segment_ids
    ):
        assert dataset_index_id == "index-1"
        assert document_id == "doc-1"
        ids = tuple(segment_ids)
        self.embedding_flushes.append(ids)
        self.events.append("embedding-completed")
        for record in self.records:
            if record.id in ids:
                record.embedding_status = "completed"

    async def activate_document_segments(self, **_kwargs):
        raise AssertionError("Task 5 must not activate segments")

    async def soft_delete_previous_segments(self, **_kwargs):
        raise AssertionError("Task 5 must not delete previous segments")


class FakeEmbedding:
    def __init__(
        self,
        events: list[str],
        *,
        dimensions: tuple[int, ...] = (3,),
        forbidden: bool = False,
        before_call: Any = None,
    ) -> None:
        self.events = events
        self.dimensions = dimensions
        self.forbidden = forbidden
        self.before_call = before_call
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def embed(self, model_id, texts):
        if self.forbidden:
            raise AssertionError("embedding must not be called")
        if self.before_call is not None:
            self.before_call(len(self.calls) + 1)
        values = tuple(texts)
        self.calls.append((model_id, values))
        self.events.append("embed")
        dimension = self.dimensions[min(len(self.calls) - 1, len(self.dimensions) - 1)]
        return EmbeddingBatch(
            vectors=tuple(tuple(float(index + 1) for index in range(dimension)) for _ in values),
            dimension=dimension,
        )


class FakeResolver:
    def __init__(
        self,
        events: list[str],
        *,
        target: VectorTarget | None = None,
        forbidden: bool = False,
    ) -> None:
        self.events = events
        self.target = target or VectorTarget("collection_building", 3)
        self.forbidden = forbidden
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, index_id, discovered_dimension):
        if self.forbidden:
            raise AssertionError("resolver must not be called")
        self.calls.append((index_id, discovered_dimension))
        self.events.append("resolve")
        return self.target


class FakeVectorStore:
    def __init__(
        self,
        events: list[str],
        *,
        fail_on_call: int | None = None,
        count_delta: int = 0,
        forbidden: bool = False,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
        retain_entities: bool = True,
    ) -> None:
        self.events = events
        self.fail_on_call = fail_on_call
        self.count_delta = count_delta
        self.forbidden = forbidden
        self.started = started
        self.release = release
        self.retain_entities = retain_entities
        self.batches: list[tuple[str, tuple[str, ...]]] = []
        self.entities: list[Any] = []
        self.entity_references: list[weakref.ReferenceType[Any]] = []
        self.thread_ids: list[int] = []

    def upsert(self, collection_name, entities):
        if self.forbidden:
            raise AssertionError("Milvus must not be called")
        self.thread_ids.append(threading.get_ident())
        values = tuple(entities)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            assert self.release.wait(timeout=2)
        call_number = len(self.batches) + 1
        if self.fail_on_call == call_number:
            self.events.append("vector-failed")
            raise VectorStoreError("VECTOR_UNAVAILABLE", True, "Vector store is unavailable.")
        self.batches.append((collection_name, tuple(entity.id for entity in values)))
        self.entity_references.extend(weakref.ref(entity) for entity in values)
        if self.retain_entities:
            self.entities.extend(values)
        self.events.append("vector-upsert")
        return len(values) + self.count_delta

    def count(self, _collection_name):
        raise AssertionError("single-document validation must not use collection count")


class FakeKeywordExtractor:
    def __init__(self, events: list[str], *, forbidden: bool = False) -> None:
        self.events = events
        self.forbidden = forbidden
        self.thread_ids: list[int] = []

    def extract(self, text, limit=15):
        if self.forbidden:
            raise AssertionError("keyword extraction must not be called")
        self.thread_ids.append(threading.get_ident())
        self.events.append("keywords")
        return [text.split()[0].lower()][:limit]


class RecordingProgress:
    def __init__(
        self,
        *,
        cancel_after_stage: str | None = None,
        cancel_after_batch: tuple[str, int] | None = None,
    ) -> None:
        self.cancel_after_stage = cancel_after_stage
        self.cancel_after_batch = cancel_after_batch
        self.updates: list[tuple[str, int, int]] = []
        self.checks = 0

    async def update(self, stage, progress, processed_segments):
        self.updates.append((stage, progress, processed_segments))

    async def check_cancelled(self):
        self.checks += 1
        if not self.updates:
            return
        stage = self.updates[-1][0]
        stage_updates = sum(1 for item in self.updates if item[0] == stage)
        if stage == self.cancel_after_stage:
            raise asyncio.CancelledError
        if self.cancel_after_batch == (stage, stage_updates):
            raise asyncio.CancelledError


class PredicateProgress(RecordingProgress):
    def __init__(self, predicate) -> None:
        super().__init__()
        self.predicate = predicate

    async def check_cancelled(self):
        await super().check_cancelled()
        if self.predicate():
            raise asyncio.CancelledError


def source_blocks(count: int = 3) -> tuple[ParsedBlock, ...]:
    return tuple(
        ParsedBlock(
            block_type="code",
            text=f"Segment {index} searchable text",
            metadata={"line": index},
        )
        for index in range(count)
    )


def staging_command(
    *, technique: str = "high_quality", mode: str = "general"
) -> SegmentStagingCommand:
    return SegmentStagingCommand(
        dataset_id="dataset-1",
        dataset_index_id="index-1",
        document_id="doc-1",
        indexing_job_id="job-1",
        indexing_technique=technique,
        segmentation_mode=mode,
    )


def high_quality_command(
    *,
    building: bool = False,
    parent_child: bool = False,
    batch_size: int = 2,
    vector_batch_size: int | None = None,
) -> IndexDocumentCommand:
    config = (
        ParentChildSegmentationConfig(
            parent_mode="paragraph",
            parent_max_length=100,
            child_max_length=100,
            child_overlap=0,
        )
        if parent_child
        else GeneralSegmentationConfig(max_chunk_length=100)
    )
    arguments = dict(
        staging=staging_command(mode="parent_child" if parent_child else "general"),
        object_key="datasets/dataset-1/documents/doc-1/source.txt",
        filename="source.txt",
        extension=".txt",
        segmentation_config=config,
        embedding_model="model-1",
        embedding_batch_size=batch_size,
        collection_name=None if building else "collection_existing",
        expected_dimension=None if building else 3,
    )
    if "vector_batch_size" in {field.name for field in fields(IndexDocumentCommand)}:
        arguments["vector_batch_size"] = (
            batch_size if vector_batch_size is None else vector_batch_size
        )
    return IndexDocumentCommand(**arguments)


def economy_command() -> IndexDocumentCommand:
    arguments = dict(
        staging=staging_command(technique="economy", mode="general"),
        object_key="datasets/dataset-1/documents/doc-1/source.txt",
        filename="source.txt",
        extension=".txt",
        segmentation_config=GeneralSegmentationConfig(max_chunk_length=100),
        embedding_model=None,
        embedding_batch_size=2,
        collection_name=None,
        expected_dimension=None,
        keyword_limit=15,
    )
    if "vector_batch_size" in {field.name for field in fields(IndexDocumentCommand)}:
        arguments["vector_batch_size"] = 2
    return IndexDocumentCommand(**arguments)


@dataclass
class Dependencies:
    events: list[str]
    storage: FakeStorage
    parser: FakeParserRegistry
    segmenter: RecordingSegmenter
    repository: FakeSegmentRepository
    embedding: FakeEmbedding
    resolver: FakeResolver
    vector_store: FakeVectorStore
    keywords: FakeKeywordExtractor


def make_engine(
    *,
    blocks: tuple[ParsedBlock, ...] | None = None,
    parser_warnings: tuple[ParserWarning, ...] = (),
    embedding_dimensions: tuple[int, ...] = (3,),
    resolved_target: VectorTarget | None = None,
    vector_fail_on_call: int | None = None,
    vector_count_delta: int = 0,
    forbidden_high_quality: bool = False,
    parser_failure: Exception | None = None,
    parser_started: threading.Event | None = None,
    parser_release: threading.Event | None = None,
    vector_started: threading.Event | None = None,
    vector_release: threading.Event | None = None,
    retain_vector_entities: bool = True,
    before_embedding_call: Any = None,
):
    events: list[str] = []
    storage = FakeStorage(events)
    parser = FakeParserRegistry(
        events,
        source_blocks() if blocks is None else blocks,
        warnings=parser_warnings,
        fail=parser_failure,
        started=parser_started,
        release=parser_release,
    )
    segmenter = RecordingSegmenter(events)
    repository = FakeSegmentRepository(events)
    embedding = FakeEmbedding(
        events,
        dimensions=embedding_dimensions,
        forbidden=forbidden_high_quality,
        before_call=before_embedding_call,
    )
    resolver = FakeResolver(
        events, target=resolved_target, forbidden=forbidden_high_quality
    )
    vector_store = FakeVectorStore(
        events,
        fail_on_call=vector_fail_on_call,
        count_delta=vector_count_delta,
        forbidden=forbidden_high_quality,
        started=vector_started,
        release=vector_release,
        retain_entities=retain_vector_entities,
    )
    keywords = FakeKeywordExtractor(events)
    deps = Dependencies(
        events,
        storage,
        parser,
        segmenter,
        repository,
        embedding,
        resolver,
        vector_store,
        keywords,
    )
    engine = DocumentIndexingEngine(
        object_storage=storage,
        parser_registry=parser,
        segmenter=segmenter,
        segment_repository=repository,
        embedding=embedding,
        vector_target_resolver=resolver,
        vector_store=vector_store,
        keyword_extractor=keywords,
    )
    return engine, deps


def mutate_staged_records(repository: FakeSegmentRepository, mutation) -> None:
    original_stage = repository.stage

    async def stage(command, segments):
        records = await original_stage(command, segments)
        mutation(records)
        return records

    repository.stage = stage


@pytest.mark.asyncio
async def test_high_quality_indexes_general_segments_and_keeps_vectors_out_of_postgres():
    engine, deps = make_engine()

    result = await engine.run(high_quality_command(), RecordingProgress())

    assert result.vector_count == result.total_indexable_segments == 3
    assert not hasattr(deps.repository.records[0], "vector")
    assert [entity.id for entity in deps.vector_store.entities] == [
        record.id for record in deps.repository.records
    ]
    assert all(record.embedding_status == "completed" for record in deps.repository.records)


@pytest.mark.asyncio
async def test_parent_child_embeds_only_children_and_leaves_parents_not_required():
    engine, deps = make_engine(blocks=source_blocks(2))

    result = await engine.run(
        high_quality_command(parent_child=True), RecordingProgress()
    )

    child_ids = {
        record.id for record in deps.repository.records if record.index_type == "child"
    }
    assert {entity.id for entity in deps.vector_store.entities} == child_ids
    assert result.total_segments == 4
    assert result.total_indexable_segments == 2
    assert all(
        record.embedding_status == "not_required"
        for record in deps.repository.records
        if record.index_type == "parent"
    )


@pytest.mark.asyncio
async def test_economy_persists_bounded_keywords_and_forbids_embedding_resolver_and_milvus():
    engine, deps = make_engine(forbidden_high_quality=True)
    progress = RecordingProgress()

    result = await engine.run(economy_command(), progress)

    assert result.vector_count == 0
    assert result.total_indexable_segments == 3
    assert all(record.keywords for record in deps.repository.records)
    assert all(len(record.keywords or []) <= 15 for record in deps.repository.records)
    assert len(deps.repository.keyword_flushes) == 1
    assert not deps.embedding.calls
    assert not deps.resolver.calls
    assert not deps.vector_store.entities


@pytest.mark.asyncio
async def test_economy_engine_with_real_repository_omits_overlong_keyword(tmp_path):
    overlong_token = "X" * 1_024
    blocks = (
        ParsedBlock(
            block_type="code",
            text=f"{overlong_token} bounded bounded",
            metadata={"line": 1},
        ),
    )
    engine, deps = make_engine(blocks=blocks, forbidden_high_quality=True)
    database_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'economy-keywords.db'}"
    )
    async with database_engine.begin() as connection:
        await connection.run_sync(DocumentSegmentRecord.__table__.create)
    factory = async_sessionmaker(database_engine, expire_on_commit=False)

    try:
        async with factory() as session:
            repository = SegmentRepository(session)
            real_engine = DocumentIndexingEngine(
                object_storage=deps.storage,
                parser_registry=deps.parser,
                segmenter=deps.segmenter,
                segment_repository=repository,
                embedding=deps.embedding,
                vector_target_resolver=deps.resolver,
                vector_store=deps.vector_store,
                keyword_extractor=KeywordExtractor(),
            )
            command = replace(
                economy_command(),
                segmentation_config=GeneralSegmentationConfig(
                    max_chunk_length=2_000
                ),
            )

            result = await real_engine.run(command, RecordingProgress())
            persisted = list(
                (
                    await session.execute(
                        select(DocumentSegmentRecord).order_by(
                            DocumentSegmentRecord.position
                        )
                    )
                ).scalars()
            )

            assert result.vector_count == 0
            assert [record.keywords for record in persisted] == [["bounded"]]
            assert deps.embedding.calls == []
            assert deps.resolver.calls == []
            assert deps.vector_store.batches == []
    finally:
        await database_engine.dispose()


@pytest.mark.asyncio
async def test_existing_target_uses_command_snapshot_without_resolving():
    engine, deps = make_engine()
    deps.resolver.forbidden = True

    await engine.run(high_quality_command(), RecordingProgress())

    assert {collection for collection, _ in deps.vector_store.batches} == {
        "collection_existing"
    }
    assert deps.resolver.calls == []


@pytest.mark.asyncio
async def test_building_target_resolves_once_from_first_embedding_dimension_before_writes():
    target = VectorTarget("collection_building", 3)
    engine, deps = make_engine(resolved_target=target)

    await engine.run(high_quality_command(building=True), RecordingProgress())

    assert deps.resolver.calls == [("index-1", 3)]
    assert deps.events.index("resolve") < deps.events.index("vector-upsert")
    assert {collection for collection, _ in deps.vector_store.batches} == {
        "collection_building"
    }


@pytest.mark.asyncio
async def test_later_embedding_dimension_mismatch_rejects_before_mismatched_batch_write():
    engine, deps = make_engine(embedding_dimensions=(3, 4))

    with pytest.raises(DocumentIndexingError) as caught:
        await engine.run(high_quality_command(), RecordingProgress())

    assert caught.value.code == "EMBEDDING_DIMENSION_MISMATCH"
    assert caught.value.retryable is False
    assert [entity.id for entity in deps.vector_store.entities] == ["rec-0", "rec-1"]
    assert [record.embedding_status for record in deps.repository.records] == [
        "completed",
        "completed",
        "waiting",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    (
        lambda records: setattr(records[0], "status", "completed"),
        lambda records: setattr(records[0], "deleted_at", object()),
        lambda records: setattr(records[0], "embedding_status", "not_required"),
    ),
    ids=("terminal-status", "soft-deleted", "technique-incompatible-state"),
)
async def test_lifecycle_ineligible_staged_rows_fail_before_vector_dependencies(mutation):
    engine, deps = make_engine()
    mutate_staged_records(deps.repository, mutation)

    with pytest.raises(DocumentIndexingError) as caught:
        await engine.run(high_quality_command(), RecordingProgress())

    assert caught.value.code == "STAGED_SEGMENT_STATE_INVALID"
    assert caught.value.retryable is False
    assert deps.embedding.calls == []
    assert deps.resolver.calls == []
    assert deps.vector_store.batches == []


@pytest.mark.asyncio
async def test_partial_high_quality_retry_skips_completed_rows_and_counts_total_ready_rows():
    engine, deps = make_engine()
    mutate_staged_records(
        deps.repository,
        lambda records: setattr(records[0], "embedding_status", "completed"),
    )

    result = await engine.run(high_quality_command(batch_size=3), RecordingProgress())

    assert result.total_indexable_segments == 3
    assert result.vector_count == 3
    assert deps.embedding.calls == [
        ("model-1", ("Segment 1 searchable text", "Segment 2 searchable text"))
    ]
    assert [batch_ids for _, batch_ids in deps.vector_store.batches] == [
        ("rec-1", "rec-2")
    ]
    assert deps.repository.embedding_flushes == [("rec-1", "rec-2")]
    assert [record.embedding_status for record in deps.repository.records] == [
        "completed",
        "completed",
        "completed",
    ]


@pytest.mark.asyncio
async def test_effective_physical_batch_uses_embedding_and_vector_snapshot_bounds():
    assert "vector_batch_size" in {
        field.name for field in fields(IndexDocumentCommand)
    }
    engine, deps = make_engine(blocks=source_blocks(1_001))
    progress = RecordingProgress(cancel_after_batch=("vector-upsert", 1))

    with pytest.raises(asyncio.CancelledError):
        await engine.run(
            high_quality_command(
                building=True,
                batch_size=512,
                vector_batch_size=500,
            ),
            progress,
        )

    assert [len(texts) for _, texts in deps.embedding.calls] == [500]
    assert [len(batch_ids) for _, batch_ids in deps.vector_store.batches] == [500]
    assert [len(batch_ids) for batch_ids in deps.repository.embedding_flushes] == [500]
    assert deps.resolver.calls == [("index-1", 3)]
    assert deps.events.index("embed") < deps.events.index("resolve")
    assert deps.events.index("resolve") < deps.events.index("vector-upsert")
    assert deps.events.index("vector-upsert") < deps.events.index("embedding-completed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "value"),
    (
        ("embedding", 513),
        ("vector", True),
        ("vector", 0),
        ("vector", 10_001),
    ),
)
async def test_embedding_and_vector_batch_snapshots_reject_invalid_bounds_before_io(
    snapshot, value
):
    engine, deps = make_engine()
    deps.storage.forbidden = True
    if snapshot == "embedding":
        command = replace(high_quality_command(), embedding_batch_size=value)
    else:
        assert "vector_batch_size" in {
            field.name for field in fields(IndexDocumentCommand)
        }
        command = high_quality_command(vector_batch_size=value)

    with pytest.raises(DocumentIndexingError) as caught:
        await engine.run(command, RecordingProgress())

    assert caught.value.code == "INDEX_COMMAND_INVALID"
    assert caught.value.retryable is False
    assert deps.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        replace(high_quality_command(), collection_name=None),
        replace(high_quality_command(), expected_dimension=None),
        replace(high_quality_command(), collection_name="unsafe/name"),
        replace(high_quality_command(), collection_name="1unsafe"),
        replace(high_quality_command(), expected_dimension=0),
        replace(high_quality_command(), expected_dimension=32_769),
        replace(high_quality_command(), embedding_model=None),
        replace(high_quality_command(), object_key="../source.txt"),
        replace(high_quality_command(), extension=".pdf"),
        replace(
            high_quality_command(),
            segmentation_config=GeneralSegmentationConfig(
                max_chunk_length=100, separator=1  # type: ignore[arg-type]
            ),
        ),
        replace(
            economy_command(),
            collection_name="collection_existing",
            expected_dimension=3,
        ),
        replace(economy_command(), embedding_model="model-1"),
        replace(
            economy_command(),
            staging=staging_command(technique="economy", mode="parent_child"),
            segmentation_config=ParentChildSegmentationConfig(
                parent_mode="paragraph",
                parent_max_length=100,
                child_max_length=50,
            ),
        ),
    ],
)
async def test_malformed_or_inconsistent_command_rejects_before_external_io(command):
    engine, deps = make_engine()
    deps.storage.forbidden = True

    with pytest.raises(DocumentIndexingError) as caught:
        await engine.run(command, RecordingProgress())

    assert caught.value.code == "INDEX_COMMAND_INVALID"
    assert caught.value.retryable is False
    assert deps.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        VectorTarget("unsafe/name", 3),
        VectorTarget("1unsafe", 3),
        VectorTarget("collection_building", 32_769),
    ],
)
async def test_malformed_resolved_target_rejects_before_vector_write(target):
    engine, deps = make_engine(resolved_target=target)

    with pytest.raises(DocumentIndexingError) as caught:
        await engine.run(high_quality_command(building=True), RecordingProgress())

    assert caught.value.code == "VECTOR_TARGET_INVALID"
    assert deps.vector_store.entities == []


@pytest.mark.asyncio
async def test_empty_parse_is_a_safe_non_retryable_failure_and_closes_storage():
    engine, deps = make_engine(blocks=())

    with pytest.raises(DocumentIndexingError) as caught:
        await engine.run(high_quality_command(), RecordingProgress())

    assert caught.value.code == "NO_EXTRACTABLE_TEXT"
    assert caught.value.retryable is False
    assert deps.storage.stream is not None and deps.storage.stream.closed
    assert "split" not in deps.events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel_after_stage", "forbidden_event"),
    [
        ("download", "parse"),
        ("parse", "split"),
        ("split", "stage"),
        ("stage", "embed"),
        ("embed-or-keywords", "vector-upsert"),
        ("vector-upsert", "validate"),
    ],
)
async def test_cancellation_is_checked_between_every_stage(
    cancel_after_stage, forbidden_event
):
    engine, deps = make_engine()
    progress = RecordingProgress(cancel_after_stage=cancel_after_stage)

    with pytest.raises(asyncio.CancelledError):
        await engine.run(high_quality_command(batch_size=8), progress)

    assert forbidden_event not in deps.events


@pytest.mark.asyncio
async def test_cancellation_between_embedding_batches_stops_before_next_batch():
    engine, deps = make_engine()
    progress = RecordingProgress(cancel_after_batch=("embed-or-keywords", 1))

    with pytest.raises(asyncio.CancelledError):
        await engine.run(high_quality_command(batch_size=1), progress)

    assert len(deps.embedding.calls) == 1
    assert deps.vector_store.entities == []


@pytest.mark.asyncio
async def test_cancellation_after_later_embedding_batch_stops_before_its_vector_batch():
    engine, deps = make_engine()
    progress = PredicateProgress(
        lambda: len(deps.embedding.calls) == 2
        and len(deps.vector_store.batches) == 1
    )

    with pytest.raises(asyncio.CancelledError):
        await engine.run(high_quality_command(batch_size=1), progress)

    assert [entity.id for entity in deps.vector_store.entities] == ["rec-0"]
    assert deps.repository.embedding_flushes == [("rec-0",)]


@pytest.mark.asyncio
async def test_cancellation_between_vector_batches_preserves_first_completed_batch():
    engine, deps = make_engine()
    progress = RecordingProgress(cancel_after_batch=("vector-upsert", 1))

    with pytest.raises(asyncio.CancelledError):
        await engine.run(high_quality_command(batch_size=1), progress)

    assert [entity.id for entity in deps.vector_store.entities] == ["rec-0"]
    assert deps.repository.embedding_flushes == [("rec-0",)]
    assert [record.embedding_status for record in deps.repository.records] == [
        "completed",
        "waiting",
        "waiting",
    ]


@pytest.mark.asyncio
async def test_cancellation_during_vector_upsert_finishes_ack_and_status_before_stopping():
    started = threading.Event()
    release = threading.Event()
    engine, deps = make_engine(
        blocks=source_blocks(1), vector_started=started, vector_release=release
    )
    task = asyncio.create_task(
        engine.run(high_quality_command(batch_size=1), RecordingProgress())
    )
    assert await asyncio.to_thread(started.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [entity.id for entity in deps.vector_store.entities] == ["rec-0"]
    assert deps.repository.embedding_flushes == [("rec-0",)]
    assert deps.repository.records[0].embedding_status == "completed"


@pytest.mark.asyncio
async def test_vector_worker_failure_wins_over_pending_cancellation():
    started = threading.Event()
    release = threading.Event()
    engine, deps = make_engine(
        blocks=source_blocks(1),
        vector_started=started,
        vector_release=release,
        vector_fail_on_call=1,
    )
    task = asyncio.create_task(
        engine.run(high_quality_command(batch_size=1), RecordingProgress())
    )
    assert await asyncio.to_thread(started.wait, 1)

    task.cancel()
    release.set()
    with pytest.raises(VectorStoreError) as caught:
        await task

    assert caught.value.code == "VECTOR_UNAVAILABLE"
    assert deps.repository.embedding_flushes == []
    assert deps.repository.records[0].embedding_status == "waiting"


@pytest.mark.asyncio
async def test_partial_vector_failure_flushes_only_previously_successful_batch_status():
    engine, deps = make_engine(vector_fail_on_call=2)

    with pytest.raises(VectorStoreError) as caught:
        await engine.run(high_quality_command(batch_size=2), RecordingProgress())

    assert caught.value.code == "VECTOR_UNAVAILABLE"
    assert caught.value.retryable is True
    assert deps.repository.embedding_flushes == [("rec-0", "rec-1")]
    assert [record.embedding_status for record in deps.repository.records] == [
        "completed",
        "completed",
        "waiting",
    ]


@pytest.mark.asyncio
async def test_upsert_count_mismatch_is_retryable_and_does_not_mark_batch_completed():
    engine, deps = make_engine(vector_count_delta=-1)

    with pytest.raises(DocumentIndexingError) as caught:
        await engine.run(high_quality_command(), RecordingProgress())

    assert caught.value.code == "VECTOR_WRITE_COUNT_MISMATCH"
    assert caught.value.retryable is True
    assert deps.repository.embedding_flushes == []
    assert all(record.embedding_status == "waiting" for record in deps.repository.records)


@pytest.mark.asyncio
async def test_storage_stream_closes_when_parser_raises_without_leaking_source_in_error():
    failure = DocumentParseError("PARSER_FAILED", "Parsing failed.", retryable=True)
    engine, deps = make_engine(parser_failure=failure)

    with pytest.raises(DocumentParseError) as caught:
        await engine.run(high_quality_command(), RecordingProgress())

    assert caught.value.code == "PARSER_FAILED"
    assert caught.value.retryable is True
    assert "secret" not in str(caught.value)
    assert deps.storage.stream is not None and deps.storage.stream.closed
    assert deps.events[-1] == "storage-closed"


@pytest.mark.asyncio
async def test_cancellation_does_not_close_storage_stream_under_running_parser_thread():
    started = threading.Event()
    release = threading.Event()
    engine, deps = make_engine(parser_started=started, parser_release=release)
    task = asyncio.create_task(engine.run(high_quality_command(), RecordingProgress()))
    assert await asyncio.to_thread(started.wait, 1)

    task.cancel()
    await asyncio.sleep(0.02)
    try:
        assert not task.done()
        assert deps.storage.stream is not None and not deps.storage.stream.closed
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert deps.storage.stream.closed


@pytest.mark.asyncio
async def test_repeated_cancellation_never_closes_stream_under_running_parser_thread():
    started = threading.Event()
    release = threading.Event()
    engine, deps = make_engine(parser_started=started, parser_release=release)
    task = asyncio.create_task(engine.run(high_quality_command(), RecordingProgress()))
    assert await asyncio.to_thread(started.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.02)
    try:
        assert not task.done()
        assert deps.storage.stream is not None and not deps.storage.stream.closed
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert deps.storage.stream.closed


@pytest.mark.asyncio
async def test_high_quality_releases_each_vector_batch_before_embedding_the_next():
    references: list[weakref.ReferenceType[Any]] = []
    events: list[str] = []

    def before_embedding(call_number: int) -> None:
        if call_number == 1:
            return
        assert events[-2:] == ["vector-upsert", "embedding-completed"]
        gc.collect()
        assert references and all(reference() is None for reference in references)

    engine, deps = make_engine(
        retain_vector_entities=False,
        before_embedding_call=before_embedding,
    )
    events = deps.events
    references = deps.vector_store.entity_references

    result = await engine.run(high_quality_command(batch_size=1), RecordingProgress())

    assert result.vector_count == 3
    assert [event for event in events if event in {"embed", "vector-upsert"}] == [
        "embed",
        "vector-upsert",
        "embed",
        "vector-upsert",
        "embed",
        "vector-upsert",
    ]
    assert deps.vector_store.entities == []


@pytest.mark.asyncio
async def test_economy_checks_cancellation_after_final_progress_before_keyword_mutation():
    engine, deps = make_engine(forbidden_high_quality=True)
    progress = RecordingProgress(cancel_after_batch=("embed-or-keywords", 2))

    with pytest.raises(asyncio.CancelledError):
        await engine.run(economy_command(), progress)

    assert deps.repository.keyword_flushes == []
    assert all(record.keywords is None for record in deps.repository.records)


@pytest.mark.asyncio
async def test_sync_parser_segmenter_and_vector_provider_run_off_event_loop_thread():
    event_loop_thread = threading.get_ident()
    engine, deps = make_engine()

    await engine.run(high_quality_command(), RecordingProgress())

    assert deps.parser.thread_ids and set(deps.parser.thread_ids) != {event_loop_thread}
    assert deps.segmenter.thread_ids and set(deps.segmenter.thread_ids) != {
        event_loop_thread
    }
    assert deps.vector_store.thread_ids and set(deps.vector_store.thread_ids) != {
        event_loop_thread
    }


@pytest.mark.asyncio
async def test_progress_is_safe_deterministic_monotonic_and_follows_exact_stage_order():
    known_warning = ParserWarning(
        code="PDF_EMPTY_PAGE",
        message="secret source fragment",
        metadata={"token": "secret-token"},
    )
    secret_code_warning = ParserWarning(
        code="APIKEY123SECRET",
        message="another source fragment",
        metadata={},
    )
    engine, _deps = make_engine(parser_warnings=(known_warning, secret_code_warning))
    progress = RecordingProgress()

    result = await engine.run(high_quality_command(), progress)

    stages = list(dict.fromkeys(stage for stage, _, _ in progress.updates))
    assert stages == [
        "download",
        "parse",
        "split",
        "stage",
        "embed-or-keywords",
        "vector-upsert",
        "validate",
    ]
    percentages = [percentage for _, percentage, _ in progress.updates]
    assert percentages == sorted(percentages)
    assert all(type(value) is int for update in progress.updates for value in update[1:])
    assert result.warnings == (
        ("parse", "PDF_EMPTY_PAGE"),
        ("parse", "INDEXING_WARNING"),
    )
    assert "secret" not in repr(progress.updates)
    assert "secret" not in repr(result)


@pytest.mark.asyncio
async def test_engine_never_activates_or_soft_deletes_segments():
    engine, deps = make_engine()

    result = await engine.run(high_quality_command(), RecordingProgress())

    assert result.total_segments == len(deps.repository.records)
    assert all(record.embedding_status == "completed" for record in deps.repository.records)
