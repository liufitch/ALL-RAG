"""Single-document indexing pipeline with explicit persistence boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable, Sequence
import inspect
import math
from pathlib import PurePosixPath
import re
from typing import Any, TypeVar

from rag_modules.parsing import ParseContext, ParserWarning
from rag_modules.segmentation import (
    GeneralSegmentationConfig,
    ParentChildSegmentationConfig,
)
from rag_modules.vector_stores.base import VectorEntity

from .models import (
    DocumentSegmenter,
    EmbeddingProvider,
    IndexDocumentCommand,
    IndexDocumentResult,
    IndexKeywordExtractor,
    IndexObjectStorage,
    IndexSegmentRecord,
    IndexSegmentRepository,
    IndexVectorStore,
    ParserDispatcher,
    ProgressReporter,
    SegmentStagingCommand,
    VectorTarget,
    VectorTargetResolver,
)

_SUPPORTED_EXTENSIONS = frozenset(
    {".txt", ".md", ".pdf", ".docx", ".xls", ".xlsx", ".csv"}
)
_COLLECTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,254}\Z")
_SAFE_WARNING_CODES = frozenset(
    {
        "EMPTY_SHEET_SKIPPED",
        "FORMULA_CACHE_UNAVAILABLE",
        "HIDDEN_SHEET_SKIPPED",
        "PARENT_FULL_DOCUMENT_FALLBACK",
        "PDF_EMPTY_PAGE",
        "SEGMENT_DELIMITER_OMITTED",
        "WARNINGS_TRUNCATED",
    }
)
_MAX_WARNINGS = 50
_MAX_COLLECTION_DIMENSION = 32_768
_MAX_BATCH_SIZE = 1_024
_MAX_KEYWORD_LIMIT = 100
_T = TypeVar("_T")


class DocumentIndexingError(Exception):
    """Stable, content-free failure created by the indexing engine."""

    def __init__(self, code: str, retryable: bool, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.retryable = retryable
        self.safe_message = safe_message


class DocumentIndexingEngine:
    """Index exactly one immutable document snapshot without activating it."""

    def __init__(
        self,
        *,
        object_storage: IndexObjectStorage,
        parser_registry: ParserDispatcher,
        segmenter: DocumentSegmenter,
        segment_repository: IndexSegmentRepository,
        embedding: EmbeddingProvider,
        vector_target_resolver: VectorTargetResolver,
        vector_store: IndexVectorStore,
        keyword_extractor: IndexKeywordExtractor,
    ) -> None:
        self._object_storage = object_storage
        self._parser_registry = parser_registry
        self._segmenter = segmenter
        self._segment_repository = segment_repository
        self._embedding = embedding
        self._vector_target_resolver = vector_target_resolver
        self._vector_store = vector_store
        self._keyword_extractor = keyword_extractor

    async def run(
        self, command: IndexDocumentCommand, progress: ProgressReporter
    ) -> IndexDocumentResult:
        self._validate_command(command)
        processed_segments = 0

        await self._check_cancelled(progress)
        async with self._object_storage.get_stream(command.object_key) as stream:
            await self._update(progress, "download", 5, processed_segments)

            await self._check_cancelled(progress)
            parsed = await self._run_sync(
                self._parser_registry.parse,
                command.extension,
                stream,
                ParseContext(
                    document_id=command.staging.document_id,
                    filename=command.filename,
                ),
            )
        await self._update(progress, "parse", 15, processed_segments)
        if not parsed.blocks:
            raise DocumentIndexingError(
                "NO_EXTRACTABLE_TEXT",
                False,
                "The document contains no extractable text.",
            )

        await self._check_cancelled(progress)
        segmented = await self._run_sync(
            self._segmenter.segment, parsed, command.segmentation_config
        )
        await self._update(progress, "split", 30, processed_segments)

        await self._check_cancelled(progress)
        records = await self._segment_repository.stage(
            command.staging, segmented.segments
        )
        await self._update(progress, "stage", 45, processed_segments)
        indexable = self._indexable_records(command.staging, records)

        warnings = self._safe_warnings(parsed.warnings, "parse")
        warnings += self._safe_warnings(segmented.warnings, "split")
        warnings = warnings[:_MAX_WARNINGS]

        if command.staging.indexing_technique == "economy":
            processed_segments = await self._extract_and_persist_keywords(
                command, indexable, progress
            )
            await self._check_cancelled(progress)
            await self._update(
                progress, "vector-upsert", 95, processed_segments
            )
            vector_count = 0
        else:
            vector_count, processed_segments = await self._index_high_quality(
                command, indexable, progress
            )

        await self._check_cancelled(progress)
        if command.staging.indexing_technique == "high_quality" and (
            vector_count != len(indexable)
        ):
            raise DocumentIndexingError(
                "VECTOR_COUNT_MISMATCH",
                True,
                "Indexed vector count did not match the document segment count.",
            )
        await self._update(progress, "validate", 100, processed_segments)
        return IndexDocumentResult(
            total_segments=len(records),
            total_indexable_segments=len(indexable),
            vector_count=vector_count,
            warnings=warnings,
        )

    async def _extract_and_persist_keywords(
        self,
        command: IndexDocumentCommand,
        records: Sequence[IndexSegmentRecord],
        progress: ProgressReporter,
    ) -> int:
        updates: dict[str, tuple[str, ...]] = {}
        batches = tuple(self._batches(records, command.embedding_batch_size))
        if not batches:
            await self._update(progress, "embed-or-keywords", 70, 0)
        for batch_index, batch in enumerate(batches, start=1):
            await self._check_cancelled(progress)
            extracted = await self._run_sync(
                self._keyword_batch, batch, command.keyword_limit
            )
            updates.update(extracted)
            completed = sum(len(item) for item in batches[:batch_index])
            await self._update(
                progress,
                "embed-or-keywords",
                self._batch_progress(45, 70, batch_index, len(batches)),
                completed,
            )
            await self._check_cancelled(progress)
        if updates:
            await self._segment_repository.update_keywords(
                dataset_index_id=command.staging.dataset_index_id,
                document_id=command.staging.document_id,
                keywords_by_segment_id=updates,
            )
        return len(records)

    def _keyword_batch(
        self, records: Sequence[IndexSegmentRecord], limit: int
    ) -> dict[str, tuple[str, ...]]:
        return {
            record.id: tuple(self._keyword_extractor.extract(record.content, limit))
            for record in records
        }

    async def _index_high_quality(
        self,
        command: IndexDocumentCommand,
        records: Sequence[IndexSegmentRecord],
        progress: ProgressReporter,
    ) -> tuple[int, int]:
        if not records:
            raise DocumentIndexingError(
                "NO_INDEXABLE_SEGMENTS",
                False,
                "The document produced no indexable segments.",
            )

        total_batches = (
            len(records) + command.embedding_batch_size - 1
        ) // command.embedding_batch_size
        target = self._command_target(command)
        locked_dimension: int | None = target.dimension if target else None
        vector_count = 0
        processed_segments = 0
        for batch_index, batch in enumerate(
            self._batches(records, command.embedding_batch_size), start=1
        ):
            await self._check_cancelled(progress)
            result = await self._embedding.embed(
                command.embedding_model or "", [record.content for record in batch]
            )
            self._validate_embedding_batch(result, len(batch))
            if locked_dimension is None:
                locked_dimension = result.dimension
                resolved = await self._vector_target_resolver.resolve(
                    command.staging.dataset_index_id, locked_dimension
                )
                self._validate_target(resolved, "VECTOR_TARGET_INVALID")
                if resolved.dimension != locked_dimension:
                    raise DocumentIndexingError(
                        "VECTOR_TARGET_DIMENSION_MISMATCH",
                        False,
                        "Resolved vector target dimension did not match embeddings.",
                    )
                target = resolved
            elif result.dimension != locked_dimension:
                raise DocumentIndexingError(
                    "EMBEDDING_DIMENSION_MISMATCH",
                    False,
                    "Embedding dimension changed while indexing the document.",
                )

            if batch_index == 1:
                await self._update(progress, "embed-or-keywords", 70, 0)
            await self._check_cancelled(progress)

            entities = tuple(
                VectorEntity(
                    id=record.id,
                    embedding=vector,
                    dataset_id=record.dataset_id,
                    document_id=record.document_id,
                    dataset_index_id=record.dataset_index_id,
                    parent_id=record.parent_id,
                    position=record.position,
                )
                for record, vector in zip(batch, result.vectors, strict=True)
            )
            assert target is not None
            written, cancellation = await self._run_sync_deferred_cancellation(
                self._vector_store.upsert, target.collection_name, entities
            )
            if isinstance(written, bool) or not isinstance(written, int) or written != len(batch):
                raise DocumentIndexingError(
                    "VECTOR_WRITE_COUNT_MISMATCH",
                    True,
                    "Vector write count did not match the submitted batch.",
                )
            _, cancellation = await self._await_deferred_cancellation(
                self._segment_repository.mark_embeddings_completed(
                    dataset_index_id=command.staging.dataset_index_id,
                    document_id=command.staging.document_id,
                    segment_ids=tuple(record.id for record in batch),
                ),
                cancellation,
            )
            vector_count += written
            processed_segments += len(batch)
            del entities
            del result
            if cancellation is not None:
                raise cancellation
            await self._update(
                progress,
                "vector-upsert",
                self._batch_progress(70, 95, batch_index, total_batches),
                processed_segments,
            )
        return vector_count, processed_segments

    @classmethod
    def _validate_command(cls, command: IndexDocumentCommand) -> None:
        invalid = not isinstance(command, IndexDocumentCommand)
        if invalid:
            cls._invalid_command()
        staging = command.staging
        if not isinstance(staging, SegmentStagingCommand):
            cls._invalid_command()
        identifiers = (
            staging.dataset_id,
            staging.dataset_index_id,
            staging.document_id,
            staging.indexing_job_id,
        )
        if any(not cls._safe_string(value, 36) for value in identifiers):
            cls._invalid_command()
        if staging.indexing_technique not in {"high_quality", "economy"}:
            cls._invalid_command()
        if staging.segmentation_mode not in {"general", "parent_child"}:
            cls._invalid_command()
        if not cls._safe_object_key(command.object_key):
            cls._invalid_command()
        if not cls._safe_filename(command.filename):
            cls._invalid_command()
        if command.extension not in _SUPPORTED_EXTENSIONS:
            cls._invalid_command()
        if PurePosixPath(command.filename).suffix.lower() != command.extension:
            cls._invalid_command()
        if (
            isinstance(command.embedding_batch_size, bool)
            or not isinstance(command.embedding_batch_size, int)
            or not 1 <= command.embedding_batch_size <= _MAX_BATCH_SIZE
        ):
            cls._invalid_command()
        if (
            isinstance(command.keyword_limit, bool)
            or not isinstance(command.keyword_limit, int)
            or not 1 <= command.keyword_limit <= _MAX_KEYWORD_LIMIT
        ):
            cls._invalid_command()

        config = command.segmentation_config
        if isinstance(config, GeneralSegmentationConfig):
            config_mode = "general"
            valid_config = (
                config.mode == "general"
                and cls._positive_int(config.max_chunk_length)
                and cls._nonnegative_int(config.overlap)
                and config.overlap < config.max_chunk_length
                and cls._safe_separator(config.separator)
            )
        elif isinstance(config, ParentChildSegmentationConfig):
            config_mode = "parent_child"
            valid_config = (
                config.mode == "parent_child"
                and config.parent_mode in {"paragraph", "full_document"}
                and cls._positive_int(config.parent_max_length)
                and cls._positive_int(config.child_max_length)
                and cls._nonnegative_int(config.child_overlap)
                and config.child_overlap < config.child_max_length
                and cls._safe_separator(config.separator)
            )
        else:
            cls._invalid_command()
            return
        if not valid_config or staging.segmentation_mode != config_mode:
            cls._invalid_command()

        target_pair = (command.collection_name is None, command.expected_dimension is None)
        if target_pair[0] != target_pair[1]:
            cls._invalid_command()
        if staging.indexing_technique == "economy":
            if (
                staging.segmentation_mode != "general"
                or command.embedding_model is not None
                or command.collection_name is not None
            ):
                cls._invalid_command()
            return
        if not cls._safe_string(command.embedding_model, 255):
            cls._invalid_command()
        if command.collection_name is not None:
            target = VectorTarget(command.collection_name, command.expected_dimension or 0)
            cls._validate_target(target, "INDEX_COMMAND_INVALID")

    @staticmethod
    def _indexable_records(
        staging: SegmentStagingCommand, records: Sequence[IndexSegmentRecord]
    ) -> list[IndexSegmentRecord]:
        if staging.indexing_technique == "economy":
            return [record for record in records if record.index_type == "general"]
        expected_type = "child" if staging.segmentation_mode == "parent_child" else "general"
        return [record for record in records if record.index_type == expected_type]

    @staticmethod
    def _validate_embedding_batch(result: Any, expected_count: int) -> None:
        if (
            not hasattr(result, "vectors")
            or not hasattr(result, "dimension")
            or not DocumentIndexingEngine._positive_int(result.dimension)
            or result.dimension > _MAX_COLLECTION_DIMENSION
            or len(result.vectors) != expected_count
            or any(
                len(vector) != result.dimension
                or any(
                    type(component) not in (int, float)
                    or not math.isfinite(float(component))
                    for component in vector
                )
                for vector in result.vectors
            )
        ):
            raise DocumentIndexingError(
                "EMBEDDING_RESULT_INVALID",
                False,
                "Embedding result was invalid.",
            )

    @staticmethod
    def _command_target(command: IndexDocumentCommand) -> VectorTarget | None:
        if command.collection_name is None:
            return None
        return VectorTarget(command.collection_name, command.expected_dimension or 0)

    @staticmethod
    def _validate_target(target: Any, error_code: str) -> None:
        if (
            not isinstance(target, VectorTarget)
            or not isinstance(target.collection_name, str)
            or len(target.collection_name) > 255
            or _COLLECTION_NAME.fullmatch(target.collection_name) is None
            or not DocumentIndexingEngine._positive_int(target.dimension)
            or target.dimension > _MAX_COLLECTION_DIMENSION
        ):
            raise DocumentIndexingError(
                error_code,
                False,
                "Vector target snapshot was invalid.",
            )

    @staticmethod
    def _safe_warnings(
        warnings: Iterable[ParserWarning], stage: str
    ) -> tuple[tuple[str, str], ...]:
        output: list[tuple[str, str]] = []
        for warning in warnings:
            code = (
                warning.code
                if warning.code in _SAFE_WARNING_CODES
                else "INDEXING_WARNING"
            )
            output.append((stage, code))
            if len(output) == _MAX_WARNINGS:
                break
        return tuple(output)

    @staticmethod
    def _batches(values: Sequence[_T], size: int) -> Iterable[Sequence[_T]]:
        for start in range(0, len(values), size):
            yield values[start : start + size]

    @staticmethod
    def _batch_progress(start: int, end: int, current: int, total: int) -> int:
        return start + ((end - start) * current // total)

    @staticmethod
    async def _update(
        progress: ProgressReporter, stage: str, percentage: int, processed: int
    ) -> None:
        await DocumentIndexingEngine._maybe_await(
            progress.update(stage, percentage, processed)
        )

    @staticmethod
    async def _check_cancelled(progress: ProgressReporter) -> None:
        await DocumentIndexingEngine._maybe_await(progress.check_cancelled())

    @staticmethod
    async def _maybe_await(value: Awaitable[None] | None) -> None:
        if inspect.isawaitable(value):
            await value

    @staticmethod
    async def _run_sync(operation: Any, *args: Any) -> Any:
        """Keep the worker's resource ownership alive until sync work exits."""
        worker = asyncio.create_task(asyncio.to_thread(operation, *args))
        result, cancellation = await DocumentIndexingEngine._wait_task(
            worker, None
        )
        if cancellation is not None:
            raise cancellation
        return result

    @staticmethod
    async def _run_sync_deferred_cancellation(
        operation: Any, *args: Any
    ) -> tuple[Any, asyncio.CancelledError | None]:
        worker = asyncio.create_task(asyncio.to_thread(operation, *args))
        return await DocumentIndexingEngine._wait_task(worker, None)

    @staticmethod
    async def _await_deferred_cancellation(
        awaitable: Awaitable[_T], cancellation: asyncio.CancelledError | None
    ) -> tuple[_T, asyncio.CancelledError | None]:
        task = asyncio.ensure_future(awaitable)
        return await DocumentIndexingEngine._wait_task(task, cancellation)

    @staticmethod
    async def _wait_task(
        task: asyncio.Future[_T], cancellation: asyncio.CancelledError | None
    ) -> tuple[_T, asyncio.CancelledError | None]:
        """Join one concrete task despite repeated outer cancellation.

        The loop is bounded by dependency-task completion. Reading the result
        before propagating cancellation deliberately preserves worker defects.
        """
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as caught:
                if cancellation is None:
                    cancellation = caught
        return task.result(), cancellation

    @staticmethod
    def _safe_string(value: Any, maximum: int) -> bool:
        return (
            isinstance(value, str)
            and 0 < len(value) <= maximum
            and value.strip() == value
            and all(ord(character) >= 32 for character in value)
        )

    @staticmethod
    def _safe_filename(value: Any) -> bool:
        return (
            DocumentIndexingEngine._safe_string(value, 255)
            and PurePosixPath(value).name == value
            and "\\" not in value
        )

    @staticmethod
    def _safe_object_key(value: Any) -> bool:
        if not DocumentIndexingEngine._safe_string(value, 1_024) or value.startswith("/"):
            return False
        if "\\" in value:
            return False
        return all(part not in {"", ".", ".."} for part in value.split("/"))

    @staticmethod
    def _positive_int(value: Any) -> bool:
        return not isinstance(value, bool) and isinstance(value, int) and value > 0

    @staticmethod
    def _nonnegative_int(value: Any) -> bool:
        return not isinstance(value, bool) and isinstance(value, int) and value >= 0

    @staticmethod
    def _safe_separator(value: Any) -> bool:
        return value is None or (
            isinstance(value, str)
            and 0 < len(value) <= 100
            and "\x00" not in value
        )

    @staticmethod
    def _invalid_command() -> None:
        raise DocumentIndexingError(
            "INDEX_COMMAND_INVALID",
            False,
            "Index document command was invalid.",
        )
