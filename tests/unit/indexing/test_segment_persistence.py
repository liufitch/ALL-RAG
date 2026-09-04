from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import traceback
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.exc import DBAPIError, DataError, IntegrityError, OperationalError, ProgrammingError

from rag_modules.common import utcnow
from rag_modules.db.models import DocumentSegmentRecord
from rag_modules.indexing.ids import segment_content_hash, stable_segment_id
from rag_modules.indexing.models import SegmentStagingCommand
import rag_modules.repositories.segment_repository as segment_repository_module
from rag_modules.repositories.segment_repository import SegmentPersistenceError, SegmentRepository
from rag_modules.segmentation.models import PreviewSegment


def command(
    *,
    dataset_index_id: str = "index-1",
    document_id: str = "doc-1",
    indexing_job_id: str = "job-1",
    indexing_technique: str = "high_quality",
    segmentation_mode: str = "parent_child",
) -> SegmentStagingCommand:
    return SegmentStagingCommand(
        dataset_id="dataset-1",
        dataset_index_id=dataset_index_id,
        document_id=document_id,
        indexing_job_id=indexing_job_id,
        indexing_technique=indexing_technique,
        segmentation_mode=segmentation_mode,
    )


def parent_child_preview_segments(*, child_first: bool = False) -> tuple[PreviewSegment, ...]:
    parent = PreviewSegment(
        local_id="parent-preview",
        parent_local_id=None,
        position=0,
        content="Parent context",
        source_metadata={"page": 1},
        index_type="parent",
    )
    child = PreviewSegment(
        local_id="child-preview",
        parent_local_id="parent-preview",
        position=1,
        content="Child retrieval text",
        source_metadata={"page": 1, "offset": 4},
        index_type="child",
    )
    return (child, parent) if child_first else (parent, child)


def general_preview_segment(
    *, content: str = "General retrieval text", position: int = 0
) -> PreviewSegment:
    return PreviewSegment(
        local_id=f"general-{position}",
        parent_local_id=None,
        position=position,
        content=content,
        source_metadata={"page": 1},
        index_type="general",
    )


@pytest_asyncio.fixture
async def segment_repository(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'segments.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(DocumentSegmentRecord.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield SegmentRepository(session)
    await engine.dispose()


@pytest_asyncio.fixture
async def race_repository(tmp_path):
    database_path = tmp_path / "segment-race.db"
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sync_engine = create_engine(f"sqlite:///{database_path}")
    async with async_engine.begin() as connection:
        await connection.run_sync(DocumentSegmentRecord.__table__.create)
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as session:
        yield SegmentRepository(session), sync_engine
    sync_engine.dispose()
    await async_engine.dispose()


def _race_record(*, record_id: str, content_hash: str, content: str) -> dict:
    return {
        "id": record_id,
        "dataset_id": "dataset-1",
        "dataset_index_id": "index-1",
        "document_id": "doc-1",
        "indexing_job_id": "other-job",
        "parent_id": None,
        "position": 0,
        "content": content,
        "content_hash": content_hash,
        "source_metadata": {"page": 1},
        "status": "indexing",
        "index_type": "general",
        "embedding_status": "waiting",
        "created_at": utcnow(),
    }


def _insert_interloper_on_first_segment_insert(
    session: AsyncSession, sync_engine, record: dict
) -> None:
    injected = False

    @event.listens_for(session.sync_session.get_bind(), "before_cursor_execute")
    def insert_interloper(_connection, _cursor, statement, *_args) -> None:
        nonlocal injected
        if injected or not statement.lstrip().upper().startswith("INSERT INTO DOCUMENT_SEGMENTS"):
            return
        injected = True
        with sync_engine.begin() as connection:
            connection.execute(DocumentSegmentRecord.__table__.insert(), record)


class _RecordedScalars:
    def __init__(self, records: list[DocumentSegmentRecord]) -> None:
        self._records = records

    def scalars(self):
        return tuple(self._records)


class _RecordingPostgresqlSession:
    """Compile real PostgreSQL statements while keeping the scale test local."""

    _INSERT_COLUMNS = (
        "id",
        "dataset_id",
        "dataset_index_id",
        "document_id",
        "indexing_job_id",
        "parent_id",
        "position",
        "content",
        "content_hash",
        "source_metadata",
        "status",
        "index_type",
        "embedding_status",
        "created_at",
    )

    def __init__(self) -> None:
        self._dialect = postgresql.dialect()
        self._records: dict[str, DocumentSegmentRecord] = {}
        self.insert_row_counts: list[int] = []
        self.insert_bind_counts: list[int] = []
        self.select_bind_counts: list[int] = []
        self.insert_sql: list[str] = []

    def get_bind(self):
        return SimpleNamespace(dialect=self._dialect)

    async def execute(self, statement):
        compiled = statement.compile(
            dialect=self._dialect,
            compile_kwargs={"render_postcompile": True},
        )
        parameters = compiled.params
        sql = str(compiled)
        if sql.lstrip().upper().startswith("INSERT INTO DOCUMENT_SEGMENTS"):
            row_indexes = sorted(
                int(name.removeprefix("id_m"))
                for name in parameters
                if name.startswith("id_m")
            )
            self.insert_row_counts.append(len(row_indexes))
            self.insert_bind_counts.append(len(parameters))
            self.insert_sql.append(sql)
            for row_index in row_indexes:
                values = {
                    column: parameters[f"{column}_m{row_index}"]
                    for column in self._INSERT_COLUMNS
                }
                self._records[values["id"]] = DocumentSegmentRecord(**values)
            return _RecordedScalars([])

        requested = {
            value
            for value in parameters.values()
            if isinstance(value, str) and value in self._records
        }
        self.select_bind_counts.append(len(parameters))
        return _RecordedScalars(
            [self._records[identifier] for identifier in requested]
        )


def _database_failure(error_type, private_content: str, private_metadata: str):
    statement = "INSERT INTO document_segments (content, source_metadata) VALUES (?, ?)"
    parameters = {
        "content": private_content,
        "source_metadata": {"marker": private_metadata},
    }
    original = RuntimeError(private_metadata)
    if error_type is DBAPIError:
        return DBAPIError(
            statement,
            parameters,
            original,
            connection_invalidated=True,
        )
    return error_type(statement, parameters, original)


def _assert_safe_storage_failure(
    error: BaseException,
    *,
    retryable: bool,
    private_values: tuple[str, str],
) -> None:
    expected_code = (
        "SEGMENT_STORAGE_UNAVAILABLE" if retryable else "SEGMENT_STORAGE_FAILED"
    )
    expected_message = (
        "Segment storage is temporarily unavailable."
        if retryable
        else "Segment storage operation failed."
    )
    storage_error_type = getattr(
        segment_repository_module, "SegmentStorageError", object()
    )

    assert error.__class__ is storage_error_type
    assert getattr(error, "code", None) == expected_code
    assert getattr(error, "retryable", None) is retryable
    assert getattr(error, "safe_message", None) == expected_message
    assert str(error) == expected_message
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    surfaces = (str(error), repr(error), rendered)
    for private_value in private_values:
        assert all(private_value not in surface for surface in surfaces)


def test_segment_id_is_stable_and_sensitive_to_index_version():
    baseline_arguments = ("index-1", "doc-1", None, 0, "hash-1")
    baseline = stable_segment_id(*baseline_arguments)
    independent_changes = (
        ("index-2", "doc-1", None, 0, "hash-1"),
        ("index-1", "doc-2", None, 0, "hash-1"),
        ("index-1", "doc-1", "parent-1", 0, "hash-1"),
        ("index-1", "doc-1", None, 1, "hash-1"),
        ("index-1", "doc-1", None, 0, "hash-2"),
    )

    parsed = UUID(hex=baseline)

    assert baseline == stable_segment_id(*baseline_arguments)
    assert all(stable_segment_id(*changed) != baseline for changed in independent_changes)
    assert len(baseline) == 32
    assert parsed.hex == baseline
    assert parsed.version == 5


def test_content_hash_canonicalizes_newlines_unicode_and_metadata_key_order():
    first = segment_content_hash(
        "cafe\u0301\r\n", {"z": ["e\u0301"], "a": {"line": "one\r\ntwo"}}
    )

    assert first == segment_content_hash(
        "caf\u00e9\n", {"a": {"line": "one\ntwo"}, "z": ["\u00e9"]}
    )


@pytest.mark.asyncio
async def test_stage_parent_child_resolves_parent_database_id(segment_repository):
    records = await segment_repository.stage(command(), parent_child_preview_segments())

    parent = next(record for record in records if record.index_type == "parent")
    child = next(record for record in records if record.index_type == "child")
    assert child.parent_id == parent.id
    assert parent.status == child.status == "indexing"
    assert parent.embedding_status == "not_required"
    assert child.embedding_status == "waiting"
    await segment_repository.session.refresh(parent)
    await segment_repository.session.refresh(child)
    assert parent.created_at is not None
    assert child.created_at is not None


@pytest.mark.asyncio
async def test_stage_resolves_a_child_listed_before_its_parent(segment_repository):
    records = await segment_repository.stage(command(), parent_child_preview_segments(child_first=True))

    assert [record.index_type for record in records] == ["child", "parent"]
    assert records[0].parent_id == records[1].id


@pytest.mark.asyncio
async def test_stage_reuses_exact_retry_ids_without_duplicates(segment_repository):
    preview = (general_preview_segment(),)
    first = await segment_repository.stage(command(segmentation_mode="general"), preview)
    second = await segment_repository.stage(
        replace(command(segmentation_mode="general"), indexing_job_id="retry-job-2"), preview
    )
    count = await segment_repository.session.scalar(
        select(func.count()).select_from(DocumentSegmentRecord)
    )

    assert [record.id for record in second] == [record.id for record in first]
    assert count == 1
    assert second[0].indexing_job_id == "job-1"


@pytest.mark.asyncio
async def test_stage_rejects_existing_id_with_wrong_hash_without_mutating_it(segment_repository):
    preview = general_preview_segment()
    expected_hash = segment_content_hash(preview.content, preview.source_metadata)
    expected_id = stable_segment_id("index-1", "doc-1", None, 0, expected_hash)
    existing = DocumentSegmentRecord(
        id=expected_id,
        dataset_id="dataset-1",
        dataset_index_id="index-1",
        document_id="doc-1",
        indexing_job_id="old-job",
        parent_id=None,
        position=0,
        content="different stored content",
        content_hash="f" * 64,
        source_metadata={"page": 1},
        status="indexing",
        index_type="general",
        embedding_status="waiting",
    )
    segment_repository.session.add(existing)
    await segment_repository.session.flush()

    with pytest.raises(SegmentPersistenceError, match="conflicts"):
        await segment_repository.stage(command(segmentation_mode="general"), (preview,))

    assert existing.content_hash == "f" * 64
    assert existing.content == "different stored content"


@pytest.mark.asyncio
async def test_stage_rejects_missing_parent_before_any_record_is_persisted(segment_repository):
    orphan = PreviewSegment(
        local_id="orphan",
        parent_local_id="missing-parent",
        position=0,
        content="orphan",
        source_metadata={},
        index_type="child",
    )

    with pytest.raises(SegmentPersistenceError, match="parent"):
        await segment_repository.stage(command(), (orphan,))

    count = await segment_repository.session.scalar(
        select(func.count()).select_from(DocumentSegmentRecord)
    )
    assert count == 0


@pytest.mark.asyncio
async def test_stage_rejects_child_reference_to_a_non_parent_before_any_record_is_persisted(
    segment_repository,
):
    general = general_preview_segment()
    child = PreviewSegment(
        local_id="child",
        parent_local_id=general.local_id,
        position=1,
        content="child",
        source_metadata={},
        index_type="child",
    )

    with pytest.raises(SegmentPersistenceError, match="parent"):
        await segment_repository.stage(command(), (general, child))

    count = await segment_repository.session.scalar(
        select(func.count()).select_from(DocumentSegmentRecord)
    )
    assert count == 0


@pytest.mark.asyncio
async def test_stage_sets_embedding_status_by_index_technique_and_segment_type(segment_repository):
    high_quality = await segment_repository.stage(
        command(segmentation_mode="general"), (general_preview_segment(),)
    )
    economy = await segment_repository.stage(
        command(
            dataset_index_id="economy-index",
            indexing_technique="economy",
            segmentation_mode="general",
        ),
        (general_preview_segment(),),
    )

    assert high_quality[0].status == economy[0].status == "indexing"
    assert high_quality[0].embedding_status == "waiting"
    assert economy[0].embedding_status == "not_required"


@pytest.mark.asyncio
async def test_update_keywords_flushes_only_exact_scoped_staged_records(segment_repository):
    target = await segment_repository.stage(
        command(indexing_technique="economy", segmentation_mode="general"),
        (general_preview_segment(content="alpha beta"),),
    )
    other = await segment_repository.stage(
        command(
            dataset_index_id="index-2",
            indexing_technique="economy",
            segmentation_mode="general",
        ),
        (general_preview_segment(content="other content"),),
    )

    await segment_repository.update_keywords(
        dataset_index_id="index-1",
        document_id="doc-1",
        keywords_by_segment_id={target[0].id: ("alpha", "beta")},
    )
    await segment_repository.session.refresh(target[0])
    await segment_repository.session.refresh(other[0])

    assert target[0].keywords == ["alpha", "beta"]
    assert other[0].keywords is None


@pytest.mark.asyncio
async def test_mark_embeddings_completed_flushes_only_submitted_batch(segment_repository):
    records = await segment_repository.stage(
        command(segmentation_mode="general"),
        (
            general_preview_segment(content="first", position=0),
            general_preview_segment(content="second", position=1),
        ),
    )

    await segment_repository.mark_embeddings_completed(
        dataset_index_id="index-1",
        document_id="doc-1",
        segment_ids=(records[0].id,),
    )
    await segment_repository.session.refresh(records[0])
    await segment_repository.session.refresh(records[1])

    assert records[0].embedding_status == "completed"
    assert records[1].embedding_status == "waiting"
    assert records[0].updated_at is not None


@pytest.mark.asyncio
async def test_keyword_batch_validation_precedes_every_mutation(segment_repository):
    records = await segment_repository.stage(
        command(indexing_technique="economy", segmentation_mode="general"),
        (
            general_preview_segment(content="first", position=0),
            general_preview_segment(content="second", position=1),
        ),
    )
    records[0].id = "a"
    records[1].id = "z"
    await segment_repository.session.flush()

    with pytest.raises(SegmentPersistenceError, match="keyword"):
        await segment_repository.update_keywords(
            dataset_index_id="index-1",
            document_id="doc-1",
            keywords_by_segment_id={
                records[0].id: ("valid",),
                records[1].id: ("",),
            },
        )

    assert records[0].keywords is None
    assert records[1].keywords is None


@pytest.mark.asyncio
async def test_embedding_batch_validation_precedes_every_mutation(segment_repository):
    records = await segment_repository.stage(
        command(), parent_child_preview_segments(child_first=True)
    )
    child = next(record for record in records if record.index_type == "child")
    parent = next(record for record in records if record.index_type == "parent")
    child.id = "a"
    parent.id = "z"
    child.parent_id = parent.id
    await segment_repository.session.flush()

    with pytest.raises(SegmentPersistenceError, match="embedding"):
        await segment_repository.mark_embeddings_completed(
            dataset_index_id="index-1",
            document_id="doc-1",
            segment_ids=(child.id, parent.id),
        )

    assert child.embedding_status == "waiting"
    assert parent.embedding_status == "not_required"


@pytest.mark.asyncio
async def test_keyword_update_rejects_high_quality_general_without_mutation(segment_repository):
    records = await segment_repository.stage(
        command(segmentation_mode="general"), (general_preview_segment(),)
    )

    with pytest.raises(SegmentPersistenceError, match="keyword"):
        await segment_repository.update_keywords(
            dataset_index_id="index-1",
            document_id="doc-1",
            keywords_by_segment_id={records[0].id: ("forbidden",)},
        )

    assert records[0].embedding_status == "waiting"
    assert records[0].keywords is None


@pytest.mark.asyncio
async def test_embedding_update_rejects_economy_general_without_mutation(segment_repository):
    records = await segment_repository.stage(
        command(indexing_technique="economy", segmentation_mode="general"),
        (general_preview_segment(),),
    )

    with pytest.raises(SegmentPersistenceError, match="embedding"):
        await segment_repository.mark_embeddings_completed(
            dataset_index_id="index-1",
            document_id="doc-1",
            segment_ids=(records[0].id,),
        )

    assert records[0].embedding_status == "not_required"
    assert records[0].keywords is None


@pytest.mark.asyncio
async def test_embedding_completion_is_idempotent_for_already_completed_batch(
    segment_repository,
):
    records = await segment_repository.stage(
        command(segmentation_mode="general"), (general_preview_segment(),)
    )
    scope = {
        "dataset_index_id": "index-1",
        "document_id": "doc-1",
        "segment_ids": (records[0].id,),
    }

    await segment_repository.mark_embeddings_completed(**scope)
    first_timestamp = records[0].updated_at
    await segment_repository.mark_embeddings_completed(**scope)

    assert records[0].embedding_status == "completed"
    assert records[0].updated_at == first_timestamp


@pytest.mark.asyncio
async def test_staging_omits_unmapped_legacy_physical_vector_column(segment_repository):
    await segment_repository.session.execute(
        text("ALTER TABLE document_segments ADD COLUMN vector BLOB NULL")
    )

    records = await segment_repository.stage(
        command(segmentation_mode="general"), (general_preview_segment(),)
    )
    physical_vector = await segment_repository.session.scalar(
        text("SELECT vector FROM document_segments WHERE id = :id"),
        {"id": records[0].id},
    )

    assert "vector" not in DocumentSegmentRecord.__table__.columns
    assert physical_vector is None


@pytest.mark.asyncio
async def test_activation_is_scoped_to_the_target_document_and_index(segment_repository):
    target = await segment_repository.stage(
        command(segmentation_mode="general"), (general_preview_segment(),)
    )
    other_document = await segment_repository.stage(
        command(document_id="doc-2", segmentation_mode="general"), (general_preview_segment(),)
    )
    other_index = await segment_repository.stage(
        command(dataset_index_id="index-2", segmentation_mode="general"), (general_preview_segment(),)
    )

    activated = await segment_repository.activate_document_segments(
        dataset_id="dataset-1", dataset_index_id="index-1", document_id="doc-1"
    )

    assert [record.id for record in activated] == [target[0].id]
    assert target[0].status == "completed"
    assert other_document[0].status == "indexing"
    assert other_index[0].status == "indexing"


@pytest.mark.asyncio
async def test_soft_delete_previous_segments_only_deletes_the_explicit_previous_version(
    segment_repository,
):
    old = await segment_repository.stage(
        command(dataset_index_id="old-index", segmentation_mode="general"), (general_preview_segment(),)
    )
    current = await segment_repository.stage(
        command(dataset_index_id="current-index", segmentation_mode="general"), (general_preview_segment(),)
    )
    await segment_repository.activate_document_segments(
        dataset_id="dataset-1", dataset_index_id="old-index", document_id="doc-1"
    )
    await segment_repository.activate_document_segments(
        dataset_id="dataset-1", dataset_index_id="current-index", document_id="doc-1"
    )

    deleted = await segment_repository.soft_delete_previous_segments(
        dataset_id="dataset-1", document_id="doc-1", previous_dataset_index_id="old-index"
    )

    assert [record.id for record in deleted] == [old[0].id]
    await segment_repository.session.refresh(old[0])
    assert old[0].deleted_at is not None
    assert current[0].deleted_at is None


@pytest.mark.asyncio
async def test_stage_rejects_economy_parent_child_configuration_before_persisting(
    segment_repository,
):
    with pytest.raises(SegmentPersistenceError, match="command"):
        await segment_repository.stage(
            command(indexing_technique="economy"), parent_child_preview_segments()
        )

    count = await segment_repository.session.scalar(
        select(func.count()).select_from(DocumentSegmentRecord)
    )
    assert count == 0


@pytest.mark.asyncio
async def test_stage_recovers_an_exact_id_race_without_poisoning_the_outer_transaction(
    race_repository,
):
    repository, sync_engine = race_repository
    preview = general_preview_segment()
    expected_hash = segment_content_hash(preview.content, preview.source_metadata)
    expected_id = stable_segment_id("index-1", "doc-1", None, 0, expected_hash)
    _insert_interloper_on_first_segment_insert(
        repository.session,
        sync_engine,
        _race_record(
            record_id=expected_id,
            content_hash=expected_hash,
            content="General retrieval text",
        ),
    )

    raced = await repository.stage(command(segmentation_mode="general"), (preview,))
    later = await repository.stage(
        command(document_id="doc-2", segmentation_mode="general"),
        (general_preview_segment(),),
    )

    assert [record.id for record in raced] == [expected_id]
    assert [record.document_id for record in later] == ["doc-2"]
    assert await repository.session.scalar(
        select(func.count()).select_from(DocumentSegmentRecord)
    ) == 2


@pytest.mark.asyncio
async def test_stage_rejects_a_conflicting_id_race_and_keeps_the_outer_transaction_usable(
    race_repository,
):
    repository, sync_engine = race_repository
    preview = general_preview_segment()
    expected_hash = segment_content_hash(preview.content, preview.source_metadata)
    expected_id = stable_segment_id("index-1", "doc-1", None, 0, expected_hash)
    _insert_interloper_on_first_segment_insert(
        repository.session,
        sync_engine,
        _race_record(
            record_id=expected_id,
            content_hash="f" * 64,
            content="conflicting interloper",
        ),
    )

    with pytest.raises(SegmentPersistenceError, match="conflicts"):
        await repository.stage(command(segmentation_mode="general"), (preview,))

    later = await repository.stage(
        command(document_id="doc-2", segmentation_mode="general"),
        (general_preview_segment(),),
    )

    assert [record.document_id for record in later] == ["doc-2"]
    assert await repository.session.scalar(
        select(func.count()).select_from(DocumentSegmentRecord)
    ) == 2


@pytest.mark.asyncio
async def test_stage_chunks_ten_thousand_postgresql_inserts_and_reloads_without_large_binds():
    session = _RecordingPostgresqlSession()
    repository = SegmentRepository(session)  # type: ignore[arg-type]
    previews = tuple(
        general_preview_segment(content=f"bounded row {position}", position=position)
        for position in range(10_000)
    )

    records = await repository.stage(command(segmentation_mode="general"), previews)

    assert len(records) == 10_000
    assert session.insert_row_counts == [500] * 20
    assert max(session.insert_bind_counts) == 7_000
    assert len(session.select_bind_counts) == 40
    assert max(session.select_bind_counts) <= 500
    assert all("ON CONFLICT (id) DO NOTHING" in sql for sql in session.insert_sql)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "retryable"),
    (
        (OperationalError, True),
        (DBAPIError, True),
        (IntegrityError, False),
        (DataError, False),
        (ProgrammingError, False),
    ),
)
async def test_stage_sanitizes_sqlalchemy_failures_and_classifies_retryability(
    error_type, retryable
):
    private_content = f"content-{uuid4().hex}"
    private_metadata = f"metadata-{uuid4().hex}"
    database_error = _database_failure(
        error_type, private_content, private_metadata
    )

    class FailingSession:
        async def execute(self, _statement):
            raise database_error

    repository = SegmentRepository(FailingSession())  # type: ignore[arg-type]
    caught: BaseException
    try:
        await repository.stage(
            command(segmentation_mode="general"),
            (general_preview_segment(content=private_content),),
        )
    except BaseException as error:
        caught = error
    else:  # pragma: no cover - assertion branch
        pytest.fail("expected storage failure")

    _assert_safe_storage_failure(
        caught,
        retryable=retryable,
        private_values=(private_content, private_metadata),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    (
        "activate_document_segments",
        "update_keywords",
        "mark_embeddings_completed",
        "soft_delete_previous_segments",
    ),
)
async def test_every_repository_mutation_sanitizes_execute_failures(operation):
    private_content = f"content-{uuid4().hex}"
    private_metadata = f"metadata-{uuid4().hex}"
    database_error = _database_failure(
        OperationalError, private_content, private_metadata
    )

    class FailingSession:
        async def execute(self, _statement):
            raise database_error

    repository = SegmentRepository(FailingSession())  # type: ignore[arg-type]
    if operation == "activate_document_segments":
        awaitable = repository.activate_document_segments(
            dataset_id="dataset-1",
            dataset_index_id="index-1",
            document_id="doc-1",
        )
    elif operation == "update_keywords":
        awaitable = repository.update_keywords(
            dataset_index_id="index-1",
            document_id="doc-1",
            keywords_by_segment_id={"segment-1": ("bounded",)},
        )
    elif operation == "mark_embeddings_completed":
        awaitable = repository.mark_embeddings_completed(
            dataset_index_id="index-1",
            document_id="doc-1",
            segment_ids=("segment-1",),
        )
    else:
        awaitable = repository.soft_delete_previous_segments(
            dataset_id="dataset-1",
            previous_dataset_index_id="index-1",
            document_id="doc-1",
        )

    try:
        await awaitable
    except BaseException as error:
        caught = error
    else:  # pragma: no cover - assertion branch
        pytest.fail("expected storage failure")

    _assert_safe_storage_failure(
        caught,
        retryable=True,
        private_values=(private_content, private_metadata),
    )


@pytest.mark.asyncio
async def test_repository_sanitizes_flush_failures_after_in_memory_validation():
    private_content = f"content-{uuid4().hex}"
    private_metadata = f"metadata-{uuid4().hex}"
    database_error = _database_failure(
        IntegrityError, private_content, private_metadata
    )
    record = DocumentSegmentRecord(
        id="segment-1",
        dataset_id="dataset-1",
        dataset_index_id="index-1",
        document_id="doc-1",
        indexing_job_id="job-1",
        parent_id=None,
        position=0,
        content=private_content,
        content_hash="f" * 64,
        source_metadata={"marker": private_metadata},
        status="indexing",
        index_type="general",
        embedding_status="not_required",
    )

    class FlushFailingSession:
        async def execute(self, _statement):
            return _RecordedScalars([record])

        async def flush(self):
            raise database_error

    repository = SegmentRepository(FlushFailingSession())  # type: ignore[arg-type]
    try:
        await repository.update_keywords(
            dataset_index_id="index-1",
            document_id="doc-1",
            keywords_by_segment_id={"segment-1": ("bounded",)},
        )
    except BaseException as error:
        caught = error
    else:  # pragma: no cover - assertion branch
        pytest.fail("expected storage failure")

    _assert_safe_storage_failure(
        caught,
        retryable=False,
        private_values=(private_content, private_metadata),
    )


@pytest.mark.asyncio
async def test_repository_preserves_non_sqlalchemy_programmer_errors():
    defect = RuntimeError("programmer defect")

    class FailingSession:
        async def execute(self, _statement):
            raise defect

    repository = SegmentRepository(FailingSession())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError) as caught:
        await repository.stage(
            command(segmentation_mode="general"),
            (general_preview_segment(),),
        )

    assert caught.value is defect


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "deleted", "embedding_status"),
    (
        ("completed", False, "waiting"),
        ("indexing", True, "waiting"),
        ("indexing", False, "not_required"),
    ),
)
async def test_stage_rejects_lifecycle_ineligible_high_quality_exact_retry(
    segment_repository, status, deleted, embedding_status
):
    preview = (general_preview_segment(),)
    first = await segment_repository.stage(command(segmentation_mode="general"), preview)
    first[0].status = status
    first[0].embedding_status = embedding_status
    first[0].deleted_at = utcnow() if deleted else None
    await segment_repository.session.flush()

    with pytest.raises(SegmentPersistenceError):
        await segment_repository.stage(command(segmentation_mode="general"), preview)


@pytest.mark.asyncio
async def test_stage_allows_partially_completed_high_quality_exact_retry_without_reattribution(
    segment_repository,
):
    previews = (
        general_preview_segment(content="already ready", position=0),
        general_preview_segment(content="still waiting", position=1),
    )
    first = await segment_repository.stage(command(segmentation_mode="general"), previews)
    first[0].embedding_status = "completed"
    await segment_repository.session.flush()

    retry = await segment_repository.stage(
        replace(command(segmentation_mode="general"), indexing_job_id="retry-job"),
        previews,
    )

    assert [record.embedding_status for record in retry] == ["completed", "waiting"]
    assert [record.indexing_job_id for record in retry] == ["job-1", "job-1"]


@pytest.mark.asyncio
async def test_stage_allows_completed_child_but_requires_not_required_parent_on_retry(
    segment_repository,
):
    previews = parent_child_preview_segments()
    first = await segment_repository.stage(command(), previews)
    parent = next(record for record in first if record.index_type == "parent")
    child = next(record for record in first if record.index_type == "child")
    child.embedding_status = "completed"
    await segment_repository.session.flush()

    retry = await segment_repository.stage(command(indexing_job_id="retry-job"), previews)

    assert [record.embedding_status for record in retry] == [
        "not_required",
        "completed",
    ]
    parent.embedding_status = "waiting"
    await segment_repository.session.flush()
    with pytest.raises(SegmentPersistenceError):
        await segment_repository.stage(command(indexing_job_id="retry-job"), previews)


@pytest.mark.asyncio
async def test_stage_requires_not_required_economy_state_on_exact_retry(
    segment_repository,
):
    economy_command = command(
        indexing_technique="economy", segmentation_mode="general"
    )
    preview = (general_preview_segment(),)
    first = await segment_repository.stage(economy_command, preview)
    first[0].embedding_status = "waiting"
    await segment_repository.session.flush()

    with pytest.raises(SegmentPersistenceError):
        await segment_repository.stage(economy_command, preview)
