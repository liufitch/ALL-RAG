from __future__ import annotations

from functools import partial

import anyio
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from rag_modules.api.dto.indexing_preview import (
    IndexingPreviewRequest,
    PreviewErrorResponse,
    PreviewResponse,
)
from rag_modules.config.settings import settings
from rag_modules.db.session import get_db_session
from rag_modules.object_storage import ObjectStorage, ObjectStorageUnavailable
from rag_modules.object_storage.factory import get_object_storage
from rag_modules.parsing.factory import get_parser_registry
from rag_modules.repositories.document_repository import DocumentRepository
from rag_modules.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag_modules.segmentation.segmenter import Segmenter
from rag_modules.services.preview_service import PreviewService, PreviewValidationError


router = APIRouter(
    prefix="/api/knowledge_base/{dataset_id}/indexing",
    tags=["Indexing"],
)


def get_preview_service(
    db=Depends(get_db_session),
    storage: ObjectStorage = Depends(get_object_storage),
) -> PreviewService:
    return PreviewService(
        repository=DocumentRepository(db),
        dataset_repository=KnowledgeBaseRepository(db),
        storage=storage,
        parser_registry=get_parser_registry(),
        segmenter=Segmenter(),
        preview_settings=settings.preview,
        upload_settings=settings.upload,
        embedding_settings=settings.embedding,
        object_storage_settings=settings.object_storage,
    )


@router.post(
    "/preview",
    response_model=PreviewResponse,
    responses={
        404: {"model": PreviewErrorResponse},
        422: {"model": PreviewErrorResponse},
        503: {"model": PreviewErrorResponse},
        504: {"model": PreviewErrorResponse},
    },
)
async def preview_documents(
    dataset_id: str,
    request: IndexingPreviewRequest,
    service: PreviewService = Depends(get_preview_service),
):
    timeout_scope = None
    try:
        with anyio.fail_after(settings.preview.timeout_seconds) as timeout_scope:
            result = await service.preview(dataset_id, request.document_ids, request)
            return await anyio.to_thread.run_sync(
                partial(_render_preview, result),
                abandon_on_cancel=True,
            )
    except PreviewValidationError as error:
        status = _domain_status(error.code)
        return JSONResponse(
            status_code=status,
            content={"code": error.code, "message": error.message},
        )
    except ObjectStorageUnavailable:
        return JSONResponse(
            status_code=503,
            content={
                "code": "OBJECT_STORAGE_UNAVAILABLE",
                "message": "Document storage is temporarily unavailable.",
            },
        )
    except TimeoutError:
        if timeout_scope is not None and timeout_scope.cancel_called:
            return JSONResponse(
                status_code=504,
                content={
                    "code": "PREVIEW_TIMEOUT",
                    "message": "The preview exceeded the configured time limit.",
                },
            )
        return _infrastructure_error()
    except (SQLAlchemyError, ConnectionError, OSError):
        return _infrastructure_error()


def _render_preview(result) -> JSONResponse:
    validated = PreviewResponse.model_validate(result)
    return JSONResponse(content=validated.model_dump(mode="json"))


def _infrastructure_error() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "code": "PREVIEW_INFRASTRUCTURE_UNAVAILABLE",
            "message": "Preview infrastructure is temporarily unavailable.",
        },
    )


def _domain_status(code: str) -> int:
    if code in {"DATASET_NOT_FOUND", "DOCUMENT_NOT_FOUND"}:
        return 404
    if code == "PREVIEW_TIMEOUT":
        return 504
    return 422
