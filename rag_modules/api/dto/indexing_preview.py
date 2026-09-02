from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


DocumentId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
ModelId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Separator = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GeneralSegmentationRequest(_StrictModel):
    mode: Literal["general"] = "general"
    max_chunk_length: int = Field(default=1024, ge=1, le=1_000_000)
    overlap: int = Field(default=100, ge=0, le=1_000_000)
    separator: Separator | None = None


class ParentChildSegmentationRequest(_StrictModel):
    mode: Literal["parent_child"] = "parent_child"
    parent_mode: Literal["paragraph", "full_document"] = "paragraph"
    parent_max_chunk_length: int = Field(default=4096, ge=1, le=1_000_000)
    child_max_chunk_length: int = Field(default=512, ge=1, le=1_000_000)
    child_overlap: int = Field(default=50, ge=0, le=1_000_000)
    separator: Separator | None = None


SegmentationRequest = Annotated[
    GeneralSegmentationRequest | ParentChildSegmentationRequest,
    Field(discriminator="mode"),
]


class IndexingPreviewRequest(_StrictModel):
    document_ids: list[DocumentId] = Field(min_length=1, max_length=100)
    indexing_technique: Literal["high_quality", "economy"]
    embedding_model: ModelId | None = None
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
    detail: Any
    request_id: str
