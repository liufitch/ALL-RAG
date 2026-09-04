"""Immutable commands and narrow ports for single-document indexing."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import AsyncContextManager, BinaryIO, Literal, Protocol, TypeAlias

from rag_modules.embeddings import EmbeddingBatch
from rag_modules.parsing import ParseContext, ParsedDocument
from rag_modules.segmentation import PreviewSegment, SegmentationConfig, SegmentationResult
from rag_modules.vector_stores.base import VectorEntity

IndexingStage: TypeAlias = Literal[
    "download",
    "parse",
    "split",
    "stage",
    "embed-or-keywords",
    "vector-upsert",
    "validate",
]
IndexingWarning: TypeAlias = tuple[Literal["parse", "split"], str]


@dataclass(frozen=True, slots=True)
class SegmentStagingCommand:
    """The immutable identity/configuration snapshot for one document staging run."""

    dataset_id: str
    dataset_index_id: str
    document_id: str
    indexing_job_id: str
    indexing_technique: Literal["high_quality", "economy"]
    segmentation_mode: Literal["general", "parent_child"]


@dataclass(frozen=True, slots=True)
class VectorTarget:
    """A validated vector collection and its immutable embedding dimension."""

    collection_name: str
    dimension: int


@dataclass(frozen=True, slots=True)
class IndexDocumentCommand:
    """Complete immutable snapshot needed to index one source document."""

    staging: SegmentStagingCommand
    object_key: str
    filename: str
    extension: str
    segmentation_config: SegmentationConfig
    embedding_model: str | None
    embedding_batch_size: int
    collection_name: str | None
    expected_dimension: int | None
    keyword_limit: int = 15


@dataclass(frozen=True, slots=True)
class IndexDocumentResult:
    """Content-free summary of one successfully validated document build."""

    total_segments: int
    total_indexable_segments: int
    vector_count: int
    warnings: tuple[IndexingWarning, ...] = ()


class IndexSegmentRecord(Protocol):
    id: str
    dataset_id: str
    dataset_index_id: str
    document_id: str
    parent_id: str | None
    position: int
    content: str
    index_type: Literal["general", "parent", "child"]
    embedding_status: Literal["waiting", "completed", "not_required"]
    keywords: list[str] | None


class ParserDispatcher(Protocol):
    def parse(
        self, extension: str, stream: BinaryIO, context: ParseContext
    ) -> ParsedDocument: ...


class IndexObjectStorage(Protocol):
    def get_stream(self, object_key: str) -> AsyncContextManager[BinaryIO]: ...


class DocumentSegmenter(Protocol):
    def segment(
        self, parsed: ParsedDocument, config: SegmentationConfig
    ) -> SegmentationResult: ...


class EmbeddingProvider(Protocol):
    async def embed(
        self, model_id: str, texts: Sequence[str]
    ) -> EmbeddingBatch: ...


class VectorTargetResolver(Protocol):
    async def resolve(
        self, index_id: str, discovered_dimension: int
    ) -> VectorTarget: ...


class IndexVectorStore(Protocol):
    def upsert(
        self, collection_name: str, entities: Sequence[VectorEntity]
    ) -> int: ...


class IndexKeywordExtractor(Protocol):
    def extract(self, text: str, limit: int = 15) -> list[str]: ...


class IndexSegmentRepository(Protocol):
    async def stage(
        self, command: SegmentStagingCommand, segments: Sequence[PreviewSegment]
    ) -> list[IndexSegmentRecord]: ...

    async def update_keywords(
        self,
        *,
        dataset_index_id: str,
        document_id: str,
        keywords_by_segment_id: Mapping[str, Sequence[str]],
    ) -> None: ...

    async def mark_embeddings_completed(
        self,
        *,
        dataset_index_id: str,
        document_id: str,
        segment_ids: Sequence[str],
    ) -> None: ...


class ProgressReporter(Protocol):
    def update(
        self, stage: IndexingStage, progress: int, processed_segments: int
    ) -> Awaitable[None] | None: ...

    def check_cancelled(self) -> Awaitable[None] | None: ...
