from __future__ import annotations

from fastapi import APIRouter

from rag_modules.api.dto.indexing_options import (
    GeneralSegmentationDefaults,
    IndexingDefaults,
    IndexingOptionsResponse,
    IndexingTechniqueOption,
    ParentChildSegmentationDefaults,
    PublicEmbeddingModel,
    PublicIndexingLimits,
    PublicParserLimits,
    PublicPreviewLimits,
    PublicUploadLimits,
    SegmentationModeOption,
)
from rag_modules.config.settings import settings

router = APIRouter(tags=["Indexing"])

PUBLIC_SUPPORTED_FILES = (
    ".txt",
    ".md",
    ".pdf",
    ".docx",
    ".xls",
    ".xlsx",
    ".csv",
)


@router.get(
    "/api/indexing/options",
    summary="Get public indexing options",
    response_model=IndexingOptionsResponse,
)
def get_indexing_options() -> IndexingOptionsResponse:
    """通过显式字段白名单返回可安全提供给前端的索引选项。"""
    return IndexingOptionsResponse(
        indexing_techniques=[
            IndexingTechniqueOption(
                id="high_quality", name="High quality", requires_embedding=True
            ),
            IndexingTechniqueOption(
                id="economy", name="Economy", requires_embedding=False
            ),
        ],
        embedding_models=[
            PublicEmbeddingModel(
                id=model.id,
                name=model.display_name,
                provider=settings.embedding.provider,
                is_default=model.id == settings.embedding.default_model,
            )
            for model in settings.embedding.models
            if model.enabled
        ],
        segmentation_modes=[
            SegmentationModeOption(
                id="general",
                name="General",
                supported_indexing_techniques=["high_quality", "economy"],
            ),
            SegmentationModeOption(
                id="parent_child",
                name="Parent-child",
                supported_indexing_techniques=["high_quality"],
            ),
        ],
        supported_files=list(PUBLIC_SUPPORTED_FILES),
        defaults=IndexingDefaults(
            indexing_technique=settings.indexing.default_indexing_technique,
            embedding_model=settings.embedding.default_model,
            general=GeneralSegmentationDefaults(
                max_chunk_length=settings.indexing.general_max_chunk_length,
                overlap=settings.indexing.general_overlap,
            ),
            parent_child=ParentChildSegmentationDefaults(
                parent_mode="paragraph",
                parent_max_chunk_length=settings.indexing.parent_max_chunk_length,
                child_max_chunk_length=settings.indexing.child_max_chunk_length,
                child_overlap=settings.indexing.child_overlap,
            ),
        ),
        limits=PublicIndexingLimits(
            upload=PublicUploadLimits(
                max_file_size_mb=settings.upload.max_file_size_mb,
                max_decompressed_size_mb=settings.upload.max_decompressed_size_mb,
            ),
            parser=PublicParserLimits(
                max_pdf_pages=settings.parser.max_pdf_pages,
                max_rows=settings.parser.max_rows,
                max_columns=settings.parser.max_columns,
                max_cell_characters=settings.parser.max_cell_characters,
            ),
            preview=PublicPreviewLimits(
                max_documents=settings.preview.max_documents,
                max_chunks=settings.preview.max_chunks,
                timeout_seconds=settings.preview.timeout_seconds,
            ),
        ),
    )
