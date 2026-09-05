"""单文档索引所用的不可变命令与职责受限的依赖接口。"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
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
    """单次文档暂存执行所用的不可变标识及配置快照。"""

    dataset_id: str
    dataset_index_id: str
    document_id: str
    indexing_job_id: str
    indexing_technique: Literal["high_quality", "economy"]
    segmentation_mode: Literal["general", "parent_child"]


@dataclass(frozen=True, slots=True)
class VectorTarget:
    """已校验的向量集合及其不可变的嵌入维度。"""

    collection_name: str
    dimension: int


@dataclass(frozen=True, slots=True)
class IndexDocumentCommand:
    """索引单个源文档所需的完整不可变快照。"""

    staging: SegmentStagingCommand
    object_key: str
    filename: str
    extension: str
    segmentation_config: SegmentationConfig
    embedding_model: str | None
    embedding_batch_size: int
    vector_batch_size: int
    collection_name: str | None
    expected_dimension: int | None
    keyword_limit: int = 15


@dataclass(frozen=True, slots=True)
class IndexDocumentResult:
    """不包含文档内容的摘要，向量数量表示所有已就绪且可建立索引的记录数。"""

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
    status: str
    deleted_at: datetime | None
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
