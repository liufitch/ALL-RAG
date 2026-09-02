import io
import time
from contextlib import asynccontextmanager
from threading import Event, Timer

import anyio
import pytest

from rag_modules.api.dto.indexing_preview import (
    GeneralSegmentationRequest,
    IndexingPreviewRequest,
)
from rag_modules.config.settings import (
    EmbeddingModelDefinition,
    EmbeddingSettings,
    PreviewSettings,
    UploadSettings,
)
from rag_modules.db.models import DocumentRecord
from rag_modules.parsing.models import ParsedBlock, ParsedDocument, ParserWarning
from rag_modules.parsing.registry import ParserRegistry
from rag_modules.parsing.text_parser import TextParser
from rag_modules.segmentation.segmenter import Segmenter
from rag_modules.services.preview_service import PreviewService, PreviewValidationError


def _record(
    document_id="doc-1",
    dataset_id="dataset-1",
    name="note.txt",
    *,
    size=1,
    key=None,
    deleted_at=None,
):
    return DocumentRecord(
        id=document_id,
        dataset_id=dataset_id,
        position=1,
        data_source_type="upload_file",
        data_source_info={
            "storage": "minio",
            "bucket": "graph-rag-uploads",
            "object_key": key
            or f"datasets/{dataset_id}/documents/{document_id}/source.txt",
            "original_filename": name,
            "size": size,
        },
        name=name,
        created_from="api",
        created_by="user-1",
        indexing_status="waiting",
        deleted_at=deleted_at,
    )


class RecordingDocuments:
    def __init__(self, records):
        self.records = records
        self.calls = []

    async def get_active_by_ids(self, dataset_id, document_ids):
        self.calls.append((dataset_id, tuple(document_ids)))
        return list(self.records)


class RecordingDataset:
    async def get_active(self, dataset_id):
        return object()


class TimedOutDataset:
    async def get_active(self, dataset_id):
        raise TimeoutError("database connection timed out")


class MemoryStorage:
    def __init__(self, objects):
        self.objects = objects
        self.keys = []

    @asynccontextmanager
    async def get_stream(self, object_key):
        self.keys.append(object_key)
        yield io.BytesIO(self.objects[object_key])

    async def get_bytes(self, object_key, max_bytes):
        self.keys.append(object_key)
        return self.objects[object_key][:max_bytes]


class SlowStorage:
    def __init__(self):
        self.entered = Event()
        self.release = Event()
        self.finished = Event()

    def _read(self, max_bytes):
        self.entered.set()
        self.release.wait(timeout=5)
        self.finished.set()
        return b"A"[:max_bytes]

    async def get_bytes(self, object_key, max_bytes):
        return await anyio.to_thread.run_sync(
            self._read,
            max_bytes,
            abandon_on_cancel=True,
        )

    @asynccontextmanager
    async def get_stream(self, object_key):
        class SlowStream:
            consumed = False

            def read(inner_self, size=-1):
                if inner_self.consumed:
                    return b""
                inner_self.consumed = True
                return self._read(size)

        yield SlowStream()


class StaticRegistry:
    def parse(self, extension, stream, context):
        return ParsedDocument(
            context.document_id,
            context.filename,
            "text",
            (
                ParsedBlock(
                    "paragraph",
                    stream.read().decode(),
                    {"line_start": 1, "line_end": 1},
                ),
            ),
            {"encoding": "utf-8"},
        )


class RecordingSegmenter(Segmenter):
    def __init__(self):
        self.calls = 0

    def segment(self, parsed, config):
        self.calls += 1
        return super().segment(parsed, config)


def _request(*ids, max_chunk_length=2):
    return IndexingPreviewRequest(
        document_ids=list(ids),
        indexing_technique="economy",
        segmentation=GeneralSegmentationRequest(
            max_chunk_length=max_chunk_length,
            overlap=0,
        ),
    )


def _service(
    records,
    objects,
    *,
    max_chunks=100,
    registry=None,
    segmenter=None,
    embedding_settings=None,
):
    return PreviewService(
        repository=RecordingDocuments(records),
        dataset_repository=RecordingDataset(),
        storage=MemoryStorage(objects),
        parser_registry=registry or StaticRegistry(),
        segmenter=segmenter or RecordingSegmenter(),
        preview_settings=PreviewSettings(
            max_documents=20,
            max_chunks=max_chunks,
            timeout_seconds=2,
        ),
        upload_settings=UploadSettings(),
        embedding_settings=embedding_settings,
    )


@pytest.mark.asyncio
async def test_preview_uses_real_parser_and_truncates_response():
    record = _record(size=len("A。B。C。D。".encode()))
    service = _service(
        [record],
        {record.data_source_info["object_key"]: "A。B。C。D。".encode()},
        max_chunks=2,
        registry=ParserRegistry({".txt": TextParser()}),
    )
    response = await service.preview(
        "dataset-1", ["doc-1"], _request("doc-1", max_chunk_length=2)
    )
    assert response.total_chunks == 4
    assert len(response.chunks) == 2
    assert response.truncated is True
    assert response.chunks[0].document_id == "doc-1"
    assert service.segmenter.calls == 1


@pytest.mark.asyncio
async def test_parent_child_total_counts_parent_and_child_records():
    record = _record(size=4)
    service = _service([record], {record.data_source_info["object_key"]: b"ABCD"})
    request = IndexingPreviewRequest(
        document_ids=["doc-1"],
        indexing_technique="high_quality",
        segmentation={
            "mode": "parent_child",
            "parent_max_chunk_length": 10,
            "child_max_chunk_length": 2,
            "child_overlap": 0,
        },
    )
    response = await service.preview("dataset-1", ["doc-1"], request)
    assert response.total_chunks == 3
    assert [chunk.index_type for chunk in response.chunks] == ["parent", "child", "child"]
    assert response.chunks[1].parent_id == "doc-1:p-000001"


@pytest.mark.asyncio
async def test_preview_deduplicates_ids_and_preserves_request_order_with_one_query():
    first, second = _record("doc-a", name="a.txt"), _record("doc-b", name="b.txt")
    service = _service(
        [second, first],
        {
            first.data_source_info["object_key"]: b"A",
            second.data_source_info["object_key"]: b"B",
        },
    )
    response = await service.preview(
        "dataset-1",
        ["doc-b", "doc-a", "doc-b"],
        _request("doc-b", "doc-a"),
    )
    assert [chunk.document_id for chunk in response.chunks] == ["doc-b", "doc-a"]
    assert service.repository.calls == [("dataset-1", ("doc-b", "doc-a"))]


@pytest.mark.asyncio
async def test_preview_rejects_missing_document_without_storage_access():
    service = _service([], {})
    with pytest.raises(PreviewValidationError) as error:
        await service.preview("dataset-1", ["missing"], _request("missing"))
    assert error.value.code == "DOCUMENT_NOT_FOUND"
    assert service.storage.keys == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata_change", "code"),
    [
        ({"object_key":"datasets/other/documents/doc-1/source.txt"}, "INVALID_SOURCE_METADATA"),
        ({"storage":"filesystem"}, "INVALID_SOURCE_METADATA"),
        ({"size": 51 * 1024 * 1024}, "FILE_SIZE_LIMIT_EXCEEDED"),
        ({"original_filename":"note.exe"}, "INVALID_SOURCE_METADATA"),
    ],
)
async def test_preview_rejects_untrusted_or_oversized_source_metadata(metadata_change, code):
    record = _record()
    record.data_source_info.update(metadata_change)
    service = _service([record], {})
    with pytest.raises(PreviewValidationError) as error:
        await service.preview("dataset-1", ["doc-1"], _request("doc-1"))
    assert error.value.code == code
    assert service.storage.keys == []


@pytest.mark.asyncio
async def test_preview_rejects_actual_size_mismatch():
    record = _record(size=1)
    service = _service([record], {record.data_source_info["object_key"]: b"AB"})
    with pytest.raises(PreviewValidationError) as error:
        await service.preview("dataset-1", ["doc-1"], _request("doc-1"))
    assert error.value.code == "INVALID_SOURCE_METADATA"


@pytest.mark.asyncio
async def test_preview_enforces_unique_document_limit_before_query():
    service = _service([], {})
    service.preview_settings = PreviewSettings(max_documents=1, max_chunks=100, timeout_seconds=2)
    with pytest.raises(PreviewValidationError) as error:
        await service.preview("dataset-1", ["a", "b", "a"], _request("a", "b"))
    assert error.value.code == "PREVIEW_DOCUMENT_LIMIT_EXCEEDED"
    assert service.repository.calls == []


@pytest.mark.asyncio
async def test_preview_rejects_oversized_repeated_document_id_list_before_deduplication():
    """Repeated IDs must not bypass request-work bounds or trigger quadratic deduplication."""
    service = _service([], {})

    with pytest.raises(PreviewValidationError) as error:
        await service.preview(
            "dataset-1",
            ["doc-1"] * 101,
            _request("doc-1"),
        )

    assert error.value.code == "PREVIEW_DOCUMENT_REFERENCE_LIMIT_EXCEEDED"
    assert service.repository.calls == []


@pytest.mark.asyncio
async def test_invalid_overlap_is_stable_and_does_not_query():
    service = _service([], {})
    request = _request("doc-1", max_chunk_length=2)
    request.segmentation.overlap = 2
    with pytest.raises(PreviewValidationError) as error:
        await service.preview("dataset-1", ["doc-1"], request)
    assert error.value.code == "INVALID_SEGMENTATION_CONFIG"
    assert service.repository.calls == []


@pytest.mark.asyncio
async def test_unknown_high_quality_model_is_stable_and_economy_does_not_resolve_model():
    embedding = EmbeddingSettings(
        default_model="enabled",
        models=[
            EmbeddingModelDefinition(id="enabled", model="m", display_name="M")
        ],
    )
    service = _service([], {}, embedding_settings=embedding)
    high_quality = IndexingPreviewRequest(
        document_ids=["doc-1"],
        indexing_technique="high_quality",
        embedding_model="missing",
        segmentation={"mode": "general"},
    )
    with pytest.raises(PreviewValidationError) as error:
        await service.preview("dataset-1", ["doc-1"], high_quality)
    assert error.value.code == "EMBEDDING_MODEL_UNAVAILABLE"
    economy = _request("doc-1")
    economy.embedding_model = "missing"
    with pytest.raises(PreviewValidationError) as missing:
        await service.preview("dataset-1", ["doc-1"], economy)
    assert missing.value.code == "DOCUMENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_explicit_empty_high_quality_model_is_not_replaced_by_default():
    service = _service([], {})
    request = IndexingPreviewRequest(
        document_ids=["doc-1"],
        indexing_technique="high_quality",
        embedding_model="bge-m3",
        segmentation={"mode": "general"},
    )
    request.embedding_model = ""

    with pytest.raises(PreviewValidationError) as error:
        await service.preview("dataset-1", ["doc-1"], request)

    assert error.value.code == "EMBEDDING_MODEL_UNAVAILABLE"
    assert service.repository.calls == []


@pytest.mark.asyncio
async def test_preview_serializes_parser_and_segmentation_warnings_and_metadata():
    class WarningRegistry:
        def parse(self, extension, stream, context):
            return ParsedDocument(
                context.document_id,
                context.filename,
                "text",
                (ParsedBlock("paragraph", "A", {"line_start": 4}),),
                {"encoding": "utf-8", "source": "fixture"},
                (
                    ParserWarning(
                        "SOURCE_WARNING", "source warning", {"line": 4}
                    ),
                ),
            )

    class WarningSegmenter(RecordingSegmenter):
        def segment(self, parsed, config):
            result = super().segment(parsed, config)
            return type(result)(
                result.segments,
                (
                    ParserWarning(
                        "SEGMENT_WARNING", "segment warning", {"mode": "general"}
                    ),
                ),
            )

    record = _record()
    service = _service(
        [record],
        {record.data_source_info["object_key"]: b"A"},
        registry=WarningRegistry(),
        segmenter=WarningSegmenter(),
    )
    response = await service.preview(
        "dataset-1", ["doc-1"], _request("doc-1", max_chunk_length=10)
    )
    assert response.documents[0].source_metadata == {
        "encoding": "utf-8",
        "source": "fixture",
    }
    assert response.chunks[0].source_metadata["line_start"] == 4
    assert response.warnings[0].model_dump() == {
        "document_id": "doc-1",
        "filename": "note.txt",
        "code": "SOURCE_WARNING",
        "message": "source warning",
        "metadata": {"line": 4},
    }
    assert response.warnings[1].code == "SEGMENT_WARNING"


@pytest.mark.asyncio
async def test_preview_never_calls_repository_writes_or_vector_provider(monkeypatch):
    import rag_modules.vector_stores.factory as vector_factory

    class ReadOnlyRepository(RecordingDocuments):
        async def create(self, record):
            raise AssertionError("preview attempted a document write")

    def forbidden_vector(*args, **kwargs):
        raise AssertionError("preview attempted vector provider access")

    monkeypatch.setattr(vector_factory, "get_vector_store", forbidden_vector)
    record = _record()
    service = _service([record], {record.data_source_info["object_key"]: b"A"})
    service.repository = ReadOnlyRepository([record])
    response = await service.preview(
        "dataset-1", ["doc-1"], _request("doc-1", max_chunk_length=10)
    )
    assert response.total_chunks == 1


@pytest.mark.asyncio
async def test_parent_child_economy_is_stable_domain_error_before_dependencies():
    request = IndexingPreviewRequest(
        document_ids=["doc-1"],
        indexing_technique="economy",
        segmentation={"mode": "parent_child"},
    )
    service = _service([], {})
    with pytest.raises(PreviewValidationError) as error:
        await service.preview("dataset-1", ["doc-1"], request)
    assert error.value.code == "PARENT_CHILD_REQUIRES_HIGH_QUALITY"
    assert service.repository.calls == []


@pytest.mark.asyncio
async def test_preview_timeout_abandons_parse_thread_safely():
    entered, release, finished = Event(), Event(), Event()

    class SlowRegistry(StaticRegistry):
        def parse(self, extension, stream, context):
            entered.set()
            release.wait(timeout=5)
            parsed = super().parse(extension, stream, context)
            finished.set()
            return parsed

    record = _record()
    service = _service(
        [record],
        {record.data_source_info["object_key"]: b"A"},
        registry=SlowRegistry(),
    )
    service.preview_settings = PreviewSettings(
        max_documents=20,
        max_chunks=100,
        timeout_seconds=1,
    )
    with pytest.raises(PreviewValidationError) as error:
        await service.preview(
            "dataset-1", ["doc-1"], _request("doc-1", max_chunk_length=10)
        )
    assert error.value.code == "PREVIEW_TIMEOUT"
    assert entered.is_set()
    release.set()
    assert finished.wait(timeout=1)


@pytest.mark.asyncio
async def test_dependency_timeout_is_not_mislabeled_as_preview_deadline():
    service = _service([], {})
    service.dataset_repository = TimedOutDataset()
    with pytest.raises(TimeoutError, match="database connection timed out"):
        await service.preview("dataset-1", ["doc-1"], _request("doc-1"))


@pytest.mark.asyncio
async def test_slow_storage_returns_preview_timeout_near_deadline():
    record = _record()
    service = _service([record], {})
    storage = SlowStorage()
    service.storage = storage
    service.preview_settings = PreviewSettings(
        max_documents=20,
        max_chunks=100,
        timeout_seconds=1,
    )
    delayed_release = Timer(1.6, storage.release.set)
    delayed_release.start()
    started = time.monotonic()

    try:
        with pytest.raises(PreviewValidationError) as error:
            await service.preview("dataset-1", ["doc-1"], _request("doc-1"))
        assert error.value.code == "PREVIEW_TIMEOUT"
        assert time.monotonic() - started < 1.4
    finally:
        storage.release.set()
        delayed_release.cancel()
        delayed_release.join(timeout=1)

    assert storage.finished.wait(timeout=1)
