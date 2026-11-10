from types import SimpleNamespace

import pytest
import asyncio
from billiard.exceptions import SoftTimeLimitExceeded

from rag_modules.tasks.indexing_tasks import IndexingTaskRunner
from tests.unit.tasks.test_indexing_tasks import FakeRepository


class Clock:
    now = 0

    def __call__(self):
        return self.now


class ResumableRepository(FakeRepository):
    async def yield_job_document(self, identifier, *, worker_id):
        self.row.status = "pending"
        self.row.attempt -= 1
        self.calls.append(("yield", identifier))


@pytest.mark.asyncio
async def test_long_job_yields_and_continues_without_exhausting_failure_attempts():
    repository = ResumableRepository()
    clock = Clock()

    class Engine:
        calls = 0

        async def run(self, command, progress):
            self.calls += 1
            if self.calls < 4:
                progress.mark_checkpoint()
                clock.now += 2
                await progress.check_cancelled()
            return SimpleNamespace(total_segments=4, total_indexable_segments=4, vector_count=4, warnings=())

    engine = Engine()
    runner = IndexingTaskRunner(repository=repository, engine=engine, command_factory=lambda _: None,
                                clock=clock, slice_seconds=1)
    for _ in range(3):
        await runner.run("jd-1")
        assert repository.row.status == "pending"
        assert repository.row.attempt == 0
        assert repository.row.error_code is None

    await runner.run("jd-1")
    assert repository.row.status == "completed"
    assert repository.row.attempt == 1
    assert not any(name == "fail" for name, _ in repository.calls)


@pytest.mark.asyncio
async def test_celery_soft_timeout_requeues_checkpoint_instead_of_failing_document():
    repository = ResumableRepository()

    class Engine:
        async def run(self, command, progress):
            progress.mark_checkpoint()
            raise SoftTimeLimitExceeded()

    await IndexingTaskRunner(repository=repository, engine=Engine(), command_factory=lambda _: None).run("jd-1")
    assert repository.row.status == "pending"
    assert repository.row.attempt == 0
    assert repository.row.error_code is None


@pytest.mark.asyncio
async def test_uncheckpointed_preparation_soft_timeout_consumes_failure_attempt():
    repository = ResumableRepository()

    class Engine:
        async def run(self, command, progress):
            progress.begin_preparation()
            raise SoftTimeLimitExceeded()

    await IndexingTaskRunner(repository=repository, engine=Engine(), command_factory=lambda _: None).run("jd-1")
    assert repository.row.status == "retry_wait"
    assert repository.row.attempt == 1
    assert repository.row.error_code == "INDEXING_PREPARATION_TIMEOUT"


@pytest.mark.asyncio
async def test_yield_keeps_committed_checkpoint_and_rolls_back_incomplete_changes(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from rag_modules.db.base import Base
    from rag_modules.db.models import IndexingJobDocumentRecord
    from rag_modules.repositories.indexing_repository import IndexingRepository

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'yield.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            row = IndexingJobDocumentRecord(id="jd", job_id="job", document_id="doc", status="running",
                                           worker_id="owner", attempt=3, progress=42, processed_segments=20)
            session.add(row)
            await session.commit()
            row.processed_segments = 999
            await session.flush()
            await IndexingRepository(session).yield_job_document("jd", worker_id="owner")
            await session.refresh(row)
            assert (row.status, row.attempt, row.processed_segments, row.progress) == ("pending", 2, 20, 42)
            assert row.worker_id is None and row.lease_expires_at is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resumed_preparation_does_not_reset_durable_progress(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from rag_modules.db.base import Base
    from rag_modules.db.models import IndexingJobDocumentRecord
    from rag_modules.repositories.indexing_repository import IndexingRepository

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'progress.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            row = IndexingJobDocumentRecord(id="jd", job_id="job", document_id="doc", status="running",
                                           worker_id="owner", progress=94, current_stage="graph-extract",
                                           processed_segments=1200, total_segments=3302)
            session.add(row)
            await session.commit()
            await IndexingRepository(session).heartbeat("jd", worker_id="owner", stage="parse", progress=10,
                                                        processed_segments=0)
            assert (row.progress, row.current_stage, row.processed_segments, row.total_segments) == (94, "graph-extract", 1200, 3302)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_observes_changes_from_another_session(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from rag_modules.common import utcnow
    from rag_modules.db.base import Base
    from rag_modules.db.models import DatasetRecord, IndexingJobRecord, IndexingJobDocumentRecord
    from rag_modules.repositories.indexing_repository import IndexingRepository

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancellation.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as worker:
            dataset = DatasetRecord(id="ds", name="D", provider="p", permission="only_me", indexing_technique="economy", created_by="u")
            job = IndexingJobRecord(id="job", dataset_id="ds", job_type="initial_index", scope="selected_documents", status="running", indexing_technique="economy", segmentation_mode="general", process_rule={}, retrieval_config={}, created_by="u")
            worker.add_all([dataset, job, IndexingJobDocumentRecord(id="jd", job_id="job", document_id="doc", status="running", worker_id="owner")])
            await worker.commit()
            repository = IndexingRepository(worker)
            assert await repository.cancellation_requested("jd") is False
            async with factory() as api:
                changed = await api.get(IndexingJobRecord, "job")
                changed.cancel_requested_at = utcnow()
                await api.commit()
            assert await repository.cancellation_requested("jd") is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_preparation_longer_than_slice_is_saved_once_and_resume_finishes():
    from dataclasses import replace
    from tests.unit.indexing.test_document_engine import make_engine, high_quality_command

    engine, deps = make_engine()
    repository = ResumableRepository()
    clock = Clock()
    prepared = False
    parse = deps.parser.parse

    def slow_parse(*args):
        clock.now += 2
        return parse(*args)

    async def save_prepared(*args, **kwargs):
        nonlocal prepared
        prepared = True

    async def load_prepared(command):
        return (deps.repository.records, ()) if prepared else None

    deps.parser.parse = slow_parse
    deps.repository.load_prepared = load_prepared
    repository.save_prepared = save_prepared
    command = replace(high_quality_command(), heartbeat_interval_seconds=.01)
    runner = IndexingTaskRunner(repository=repository, engine=engine, command_factory=lambda _: command,
                                slice_seconds=1, clock=clock)
    await runner.run("jd-1")
    assert prepared
    assert len(deps.repository.records) == 3
    assert repository.row.status == "pending"
    await runner.run("jd-1")
    assert repository.row.status == "completed"
    assert len(deps.parser.contexts) == 1


@pytest.mark.asyncio
async def test_economy_keyword_batches_survive_time_slice_and_are_not_recomputed():
    from tests.unit.indexing.test_document_engine import make_engine, economy_command

    engine, deps = make_engine(forbidden_high_quality=True)
    repository = ResumableRepository()
    clock = Clock()
    prepared = False
    extract = deps.keywords.extract
    calls = []

    def slow_extract(content, limit):
        calls.append(content)
        clock.now += .6
        return extract(content, limit)

    async def save_prepared(*args, **kwargs):
        nonlocal prepared
        prepared = True

    async def load_prepared(command):
        return (deps.repository.records, ()) if prepared else None

    deps.keywords.extract = slow_extract
    deps.repository.load_prepared = load_prepared
    repository.save_prepared = save_prepared
    runner = IndexingTaskRunner(repository=repository, engine=engine, command_factory=lambda _: economy_command(),
                                slice_seconds=1, clock=clock)
    await runner.run("jd-1")
    assert repository.row.status == "pending"
    assert sum(row.keywords is not None for row in deps.repository.records) == 2
    await runner.run("jd-1")
    assert repository.row.status == "completed"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_durable_preparation_reloads_only_its_own_staged_segments_and_warnings(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from rag_modules.db.base import Base
    from rag_modules.db.models import IndexingJobDocumentRecord
    from rag_modules.indexing.models import SegmentStagingCommand
    from rag_modules.repositories.indexing_repository import IndexingRepository
    from rag_modules.repositories.segment_repository import SegmentRepository
    from rag_modules.segmentation import PreviewSegment

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'prepared.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(IndexingJobDocumentRecord(id="jd", job_id="job", document_id="doc", status="running", worker_id="owner"))
            await session.commit()
            command = SegmentStagingCommand("ds", "index", "doc", "job", "economy", "general")
            segments = SegmentRepository(session)
            assert await segments.load_prepared(command) is None
            await segments.stage(command, [PreviewSegment(local_id="s", parent_local_id=None, position=0,
                                                          content="durable content", source_metadata={}, index_type="general")])
            assert await segments.load_prepared(command) is None
            await IndexingRepository(session).save_prepared("jd", worker_id="owner", warnings=[{"stage": "parse", "code": "PDF_EMPTY_PAGE"}])
            records, warnings = await segments.load_prepared(command)
            assert [record.content for record in records] == ["durable content"]
            assert warnings == (("parse", "PDF_EMPTY_PAGE"),)
            assert await segments.load_prepared(SegmentStagingCommand("ds", "index", "doc", "other-job", "economy", "general")) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_single_request_longer_than_slice_still_makes_forward_progress():
    from dataclasses import replace
    from tests.unit.indexing.test_document_engine import make_engine, high_quality_command

    engine, deps = make_engine()
    repository = ResumableRepository()
    prepared = False
    embed = deps.embedding.embed

    async def slow_embed(*args):
        await asyncio.sleep(.03)
        return await embed(*args)

    async def save_prepared(*args, **kwargs):
        nonlocal prepared
        prepared = True

    async def load_prepared(command):
        return (deps.repository.records, ()) if prepared else None

    deps.embedding.embed = slow_embed
    deps.repository.load_prepared = load_prepared
    repository.save_prepared = save_prepared
    command = replace(high_quality_command(), heartbeat_interval_seconds=.005)
    runner = IndexingTaskRunner(repository=repository, engine=engine, command_factory=lambda _: command, slice_seconds=.01)
    for _ in range(4):
        await runner.run("jd-1")
        if repository.row.status == "completed":
            break
    assert repository.row.status == "completed"
    assert sum(len(texts) for _, texts in deps.embedding.calls) == 3
