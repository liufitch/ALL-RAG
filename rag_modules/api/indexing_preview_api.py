from __future__ import annotations

from functools import partial
import re
from typing import Annotated, Any
from uuid import uuid4

import anyio
from fastapi import APIRouter, Depends, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
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


_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_REQUEST_ID_RESPONSE_HEADER = {
    "X-Request-ID": {
        "description": "Correlation identifier matching request_id in an error body.",
        "schema": {"type": "string"},
    }
}


class PreviewAPIRoute(APIRoute):
    """将预览接口的错误契约应用于处理函数执行前的参数校验。"""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def preview_route_handler(request: Request):
            request.state.preview_request_id = _request_id(request)
            try:
                response = await original(request)
            except RequestValidationError as error:
                response = _error_response(
                    request,
                    422,
                    "PREVIEW_REQUEST_VALIDATION_FAILED",
                    "The preview request is invalid.",
                    _safe_validation_detail(error),
                )
            except ObjectStorageUnavailable:
                response = _error_response(
                    request,
                    503,
                    "OBJECT_STORAGE_UNAVAILABLE",
                    "Document storage is temporarily unavailable.",
                )
            except (SQLAlchemyError, ConnectionError, OSError, TimeoutError):
                response = _infrastructure_error(request)
            response.headers["X-Request-ID"] = request.state.preview_request_id
            return response

        return preview_route_handler


router = APIRouter(
    prefix="/api/knowledge_base/{dataset_id}/indexing",
    tags=["Indexing"],
    route_class=PreviewAPIRoute,
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
        404: {"model": PreviewErrorResponse, "headers": _REQUEST_ID_RESPONSE_HEADER},
        422: {"model": PreviewErrorResponse, "headers": _REQUEST_ID_RESPONSE_HEADER},
        503: {"model": PreviewErrorResponse, "headers": _REQUEST_ID_RESPONSE_HEADER},
        504: {"model": PreviewErrorResponse, "headers": _REQUEST_ID_RESPONSE_HEADER},
    },
)
async def preview_documents(
    dataset_id: Annotated[
        str,
        Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
    ],
    request: Request,
    preview_request: IndexingPreviewRequest,
    service: PreviewService = Depends(get_preview_service),
):
    timeout_scope = None
    try:
        with anyio.fail_after(settings.preview.timeout_seconds) as timeout_scope:
            result = await service.preview(
                dataset_id, preview_request.document_ids, preview_request
            )
            return await anyio.to_thread.run_sync(
                partial(
                    _render_preview,
                    result,
                    request.state.preview_request_id,
                ),
                abandon_on_cancel=True,
            )
    except PreviewValidationError as error:
        status = _domain_status(error.code)
        return _error_response(
            request,
            status,
            error.code,
            error.message,
        )
    except ObjectStorageUnavailable:
        return _error_response(
            request,
            503,
            "OBJECT_STORAGE_UNAVAILABLE",
            "Document storage is temporarily unavailable.",
        )
    except TimeoutError:
        if timeout_scope is not None and timeout_scope.cancel_called:
            return _error_response(
                request,
                504,
                "PREVIEW_TIMEOUT",
                "The preview exceeded the configured time limit.",
            )
        return _infrastructure_error(request)
    except (SQLAlchemyError, ConnectionError, OSError):
        return _infrastructure_error(request)


def _render_preview(result, request_id: str) -> JSONResponse:
    validated = PreviewResponse.model_validate(result)
    return JSONResponse(
        content=validated.model_dump(mode="json"),
        headers={"X-Request-ID": request_id},
    )


def _infrastructure_error(request: Request) -> JSONResponse:
    return _error_response(
        request,
        503,
        "PREVIEW_INFRASTRUCTURE_UNAVAILABLE",
        "Preview infrastructure is temporarily unavailable.",
    )


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    detail: Any = None,
) -> JSONResponse:
    request_id = getattr(request.state, "preview_request_id", _request_id(request))
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "detail": detail,
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )


def _request_id(request: Request) -> str:
    supplied = request.headers.get("X-Request-ID", "")
    return supplied if _REQUEST_ID_PATTERN.fullmatch(supplied) else uuid4().hex


def _safe_validation_detail(error: RequestValidationError) -> list[dict[str, Any]]:
    return [
        {
            "location": list(item.get("loc", ())),
            "message": str(item.get("msg", "Invalid value."))[:256],
            "type": str(item.get("type", "value_error"))[:128],
        }
        for item in error.errors()[:100]
    ]


def _domain_status(code: str) -> int:
    if code in {"DATASET_NOT_FOUND", "DOCUMENT_NOT_FOUND"}:
        return 404
    if code == "PREVIEW_TIMEOUT":
        return 504
    return 422
