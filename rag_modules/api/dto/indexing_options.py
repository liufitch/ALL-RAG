from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class IndexingTechniqueOption(BaseModel):
    id: Literal["high_quality", "economy"]
    name: str
    requires_embedding: bool


class PublicEmbeddingModel(BaseModel):
    id: str
    name: str
    provider: Literal["openai_compatible"]
    is_default: bool


class SegmentationModeOption(BaseModel):
    id: Literal["general", "parent_child"]
    name: str
    supported_indexing_techniques: list[Literal["high_quality", "economy"]]


class GeneralSegmentationDefaults(BaseModel):
    max_chunk_length: int
    overlap: int


class ParentChildSegmentationDefaults(BaseModel):
    parent_mode: Literal["paragraph"]
    parent_max_chunk_length: int
    child_max_chunk_length: int
    child_overlap: int


class IndexingDefaults(BaseModel):
    indexing_technique: Literal["high_quality", "economy"]
    embedding_model: str
    general: GeneralSegmentationDefaults
    parent_child: ParentChildSegmentationDefaults


class PublicUploadLimits(BaseModel):
    max_file_size_mb: int
    max_decompressed_size_mb: int


class PublicParserLimits(BaseModel):
    max_pdf_pages: int
    max_rows: int
    max_columns: int
    max_cell_characters: int


class PublicPreviewLimits(BaseModel):
    max_documents: int
    max_chunks: int
    timeout_seconds: int


class PublicIndexingLimits(BaseModel):
    upload: PublicUploadLimits
    parser: PublicParserLimits
    preview: PublicPreviewLimits


class IndexingOptionsResponse(BaseModel):
    indexing_techniques: list[IndexingTechniqueOption]
    embedding_models: list[PublicEmbeddingModel]
    segmentation_modes: list[SegmentationModeOption]
    supported_files: list[str]
    defaults: IndexingDefaults
    limits: PublicIndexingLimits
