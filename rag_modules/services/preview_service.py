from __future__ import annotations

import io
from functools import partial
from pathlib import Path

import anyio

from rag_modules.api.dto.indexing_preview import (
    GeneralSegmentationRequest,
    IndexingPreviewRequest,
    ParentChildSegmentationRequest,
    PreviewChunk,
    PreviewDocument,
    PreviewResponse,
    PreviewWarning,
)
from rag_modules.config.settings import (
    EmbeddingSettings,
    ObjectStorageSettings,
    PreviewSettings,
    UploadSettings,
)
from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import DocumentParseError, ParserWarning
from rag_modules.parsing.warnings import BoundedWarningCollector
from rag_modules.segmentation.models import (
    GeneralSegmentationConfig,
    ParentChildSegmentationConfig,
    SegmentationConfigError,
)
from rag_modules.upload_formats import SUPPORTED_UPLOAD_EXTENSIONS


class PreviewValidationError(ValueError):
    """API 边界对外暴露的稳定预览异常，可安全返回客户端。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class PreviewService:
    """读取、解析并分段已存储的源文档，不执行持久化。"""

    def __init__(
        self,
        repository,
        dataset_repository,
        storage,
        parser_registry,
        segmenter,
        preview_settings: PreviewSettings,
        upload_settings: UploadSettings,
        embedding_settings: EmbeddingSettings | None = None,
        object_storage_settings: ObjectStorageSettings | None = None,
    ) -> None:
        self.repository = repository
        self.dataset_repository = dataset_repository
        self.storage = storage
        self.parser_registry = parser_registry
        self.segmenter = segmenter
        self.preview_settings = preview_settings
        self.upload_settings = upload_settings
        self.embedding_settings = embedding_settings or EmbeddingSettings()
        self.object_storage_settings = object_storage_settings or ObjectStorageSettings()

    async def preview(
        self,
        dataset_id: str,
        document_ids: list[str],
        request: IndexingPreviewRequest,
    ) -> PreviewResponse:
        if len(document_ids) > 100:
            raise PreviewValidationError(
                "PREVIEW_DOCUMENT_REFERENCE_LIMIT_EXCEEDED",
                "Too many document references were supplied for preview.",
            )
        normalized_ids = _unique_document_ids(document_ids)
        self._validate_request(normalized_ids, request)
        timeout_scope = None
        try:
            with anyio.fail_after(self.preview_settings.timeout_seconds) as timeout_scope:
                dataset = await self.dataset_repository.get_active(dataset_id)
                if dataset is None:
                    raise PreviewValidationError(
                        "DATASET_NOT_FOUND", "The knowledge base does not exist."
                    )
                records = await self.repository.get_active_by_ids(
                    dataset_id, normalized_ids
                )
                by_id = {record.id: record for record in records}
                if len(by_id) != len(normalized_ids):
                    raise PreviewValidationError(
                        "DOCUMENT_NOT_FOUND",
                        "One or more documents do not belong to the knowledge base.",
                    )

                chunks: list[PreviewChunk] = []
                warnings = BoundedWarningCollector[PreviewWarning](
                    self.preview_settings.max_warnings,
                    _preview_warnings_truncated,
                )
                documents: list[PreviewDocument] = []
                total_chunks = 0
                config = _segmentation_config(request.segmentation)

                for document_id in normalized_ids:
                    record = by_id[document_id]
                    filename, extension, object_key, size = self._source(record, dataset_id)
                    payload = await self._read_source(object_key, size)
                    try:
                        parsed, segmented = await anyio.to_thread.run_sync(
                            partial(
                                _parse_and_segment,
                                self.parser_registry,
                                self.segmenter,
                                extension,
                                payload,
                                document_id,
                                filename,
                                config,
                            ),
                            abandon_on_cancel=True,
                        )
                    except DocumentParseError as error:
                        raise PreviewValidationError(error.code, error.message) from error
                    except SegmentationConfigError as error:
                        raise PreviewValidationError(error.code, error.message) from error

                    documents.append(
                        PreviewDocument(
                            document_id=document_id,
                            filename=filename,
                            source_type=parsed.source_type,
                            source_metadata=parsed.metadata,
                        )
                    )
                    warnings.extend(
                        _warning(document_id, filename, warning)
                        for warning in parsed.warnings
                    )
                    warnings.extend(
                        _warning(document_id, filename, warning)
                        for warning in segmented.warnings
                    )
                    total_chunks += len(segmented.segments)
                    available = self.preview_settings.max_chunks - len(chunks)
                    if available > 0:
                        chunks.extend(
                            _chunk(document_id, segment)
                            for segment in segmented.segments[:available]
                        )

                return PreviewResponse(
                    chunks=chunks,
                    total_chunks=total_chunks,
                    truncated=total_chunks > len(chunks),
                    warnings=list(warnings.result()),
                    documents=documents,
                )
        except TimeoutError as error:
            if timeout_scope is not None and timeout_scope.cancel_called:
                raise PreviewValidationError(
                    "PREVIEW_TIMEOUT",
                    "The preview exceeded the configured time limit.",
                ) from error
            raise

    def _validate_request(
        self, document_ids: list[str], request: IndexingPreviewRequest
    ) -> None:
        if len(document_ids) > self.preview_settings.max_documents:
            raise PreviewValidationError(
                "PREVIEW_DOCUMENT_LIMIT_EXCEEDED",
                "Too many documents were selected for preview.",
            )
        if (
            isinstance(request.segmentation, ParentChildSegmentationRequest)
            and request.indexing_technique == "economy"
        ):
            raise PreviewValidationError(
                "PARENT_CHILD_REQUIRES_HIGH_QUALITY",
                "Parent-child segmentation requires high-quality indexing.",
            )
        if request.indexing_technique == "high_quality":
            model_id = (
                self.embedding_settings.default_model
                if request.embedding_model is None
                else request.embedding_model
            )
            try:
                self.embedding_settings.get_model(model_id)
            except ValueError as error:
                raise PreviewValidationError(
                    "EMBEDDING_MODEL_UNAVAILABLE",
                    "The embedding model is unknown or disabled.",
                ) from error
        _validate_segmentation(_segmentation_config(request.segmentation))

    def _source(self, record, dataset_id: str) -> tuple[str, str, str, int]:
        info = record.data_source_info
        if not isinstance(info, dict):
            raise _invalid_source_metadata()
        filename = info.get("original_filename")
        object_key = info.get("object_key")
        size = info.get("size")
        if (
            info.get("storage") != "minio"
            or info.get("bucket") != self.object_storage_settings.bucket
            or not isinstance(filename, str)
            or filename != record.name
            or not isinstance(object_key, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise _invalid_source_metadata()
        extension = Path(filename).suffix.lower()
        approved = set(self.upload_settings.allowed_extensions).intersection(
            SUPPORTED_UPLOAD_EXTENSIONS
        )
        expected_key = (
            f"datasets/{dataset_id}/documents/{record.id}/source{extension}"
        )
        if extension not in approved or object_key != expected_key:
            raise _invalid_source_metadata()
        if size > self.upload_settings.max_file_size_mb * 1024 * 1024:
            raise PreviewValidationError(
                "FILE_SIZE_LIMIT_EXCEEDED",
                "The stored document exceeds the configured preview size limit.",
            )
        return filename, extension, object_key, size

    async def _read_source(self, object_key: str, expected_size: int) -> bytes:
        payload = await self.storage.get_bytes(object_key, expected_size + 1)
        if len(payload) != expected_size:
            raise PreviewValidationError(
                "INVALID_SOURCE_METADATA",
                "The stored document size does not match its metadata.",
            )
        return payload


def _unique_document_ids(document_ids: list[str]) -> list[str]:
    normalized: list[str] = []
    for document_id in document_ids:
        if not isinstance(document_id, str) or not document_id.strip():
            raise PreviewValidationError(
                "INVALID_DOCUMENT_ID", "Document IDs must be non-empty strings."
            )
        identifier = document_id.strip()
        if identifier not in normalized:
            normalized.append(identifier)
    return normalized


def _segmentation_config(request):
    if isinstance(request, GeneralSegmentationRequest):
        return GeneralSegmentationConfig(
            max_chunk_length=request.max_chunk_length,
            overlap=request.overlap,
            separator=request.separator,
        )
    return ParentChildSegmentationConfig(
        parent_mode=request.parent_mode,
        parent_max_length=request.parent_max_chunk_length,
        child_max_length=request.child_max_chunk_length,
        child_overlap=request.child_overlap,
        separator=request.separator,
    )


def _validate_segmentation(config) -> None:
    if isinstance(config, GeneralSegmentationConfig):
        valid = (
            config.max_chunk_length > 0
            and 0 <= config.overlap < config.max_chunk_length
        )
    else:
        valid = (
            config.parent_max_length > 0
            and config.child_max_length > 0
            and 0 <= config.child_overlap < config.child_max_length
        )
    if not valid:
        raise PreviewValidationError(
            "INVALID_SEGMENTATION_CONFIG",
            "Segmentation lengths and overlaps are invalid.",
        )


def _invalid_source_metadata() -> PreviewValidationError:
    return PreviewValidationError(
        "INVALID_SOURCE_METADATA", "The document source metadata is invalid."
    )


def _parse_and_segment(
    registry, segmenter, extension, payload, document_id, filename, config
):
    parsed = registry.parse(
        extension,
        io.BytesIO(payload),
        ParseContext(document_id=document_id, filename=filename),
    )
    return parsed, segmenter.segment(parsed, config)


def _warning(document_id: str, filename: str, warning: ParserWarning) -> PreviewWarning:
    return PreviewWarning(
        document_id=document_id,
        filename=filename,
        code=warning.code,
        message=warning.message,
        metadata=warning.metadata,
    )


def _preview_warnings_truncated(omitted_count: int) -> PreviewWarning:
    return PreviewWarning(
        document_id="",
        filename="",
        code="WARNINGS_TRUNCATED",
        message="Additional warnings were omitted.",
        metadata={"omitted_count": omitted_count},
    )


def _chunk(document_id: str, segment) -> PreviewChunk:
    parent_id = (
        f"{document_id}:{segment.parent_local_id}"
        if segment.parent_local_id is not None
        else None
    )
    return PreviewChunk(
        id=f"{document_id}:{segment.local_id}",
        document_id=document_id,
        local_id=segment.local_id,
        parent_id=parent_id,
        position=segment.position,
        content=segment.content,
        source_metadata=segment.source_metadata,
        index_type=segment.index_type,
    )
