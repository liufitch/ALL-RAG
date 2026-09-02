from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GeneralSegmentationRequest(_StrictModel):
    mode: Literal["general"] = "general"
    max_chunk_length: int = 1024
    overlap: int = 100
    separator: str | None = None


class ParentChildSegmentationRequest(_StrictModel):
    mode: Literal["parent_child"] = "parent_child"
    parent_mode: Literal["paragraph", "full_document"] = "paragraph"
    parent_max_chunk_length: int = 4096
    child_max_chunk_length: int = 512
    child_overlap: int = 50
    separator: str | None = None


SegmentationRequest = Annotated[
    GeneralSegmentationRequest | ParentChildSegmentationRequest,
    Field(discriminator="mode"),
]


class IndexingPreviewRequest(_StrictModel):
    document_ids: list[str] = Field(min_length=1)
    indexing_technique: Literal["high_quality", "economy"]
    embedding_model: str | None = None
    segmentation: SegmentationRequest


class PreviewChunk(_StrictModel):
    id: str
    document_id: str
    local_id: str
    parent_id: str | None
    position: int
    content: str
    source_metadata: dict[str, Any]
    index_type: Literal["general", "parent", "child"]


class PreviewWarning(_StrictModel):
    document_id: str
    filename: str
    code: str
    message: str
    metadata: dict[str, Any]


class PreviewDocument(_StrictModel):
    document_id: str
    filename: str
    source_type: str
    source_metadata: dict[str, Any]


class PreviewResponse(_StrictModel):
    chunks: list[PreviewChunk]
    total_chunks: int
    truncated: bool
    warnings: list[PreviewWarning]
    documents: list[PreviewDocument]


class PreviewErrorResponse(_StrictModel):
    code: str
    message: str
