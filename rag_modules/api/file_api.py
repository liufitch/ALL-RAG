from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.exc import SQLAlchemyError

from rag_modules.api.dto.document import (
    DocumentItem,
    DocumentListResponse,
    DocumentRejection,
    DocumentUploadResponse,
)
from rag_modules.config.settings import settings
from rag_modules.db.session import get_db_session
from rag_modules.documents.types import UploadValidationError
from rag_modules.object_storage import ObjectStorage, ObjectStorageUnavailable
from rag_modules.object_storage.factory import get_object_storage
from rag_modules.repositories.document_repository import DocumentRepository
from rag_modules.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag_modules.services.document_service import DatasetNotFoundError, DocumentService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/knowledge_base/{dataset_id}/documents",
    tags=["文档管理"],
)


def get_document_service(
    db=Depends(get_db_session),
    storage: ObjectStorage = Depends(get_object_storage),
) -> DocumentService:
    return DocumentService(
        repository=DocumentRepository(db),
        dataset_repository=KnowledgeBaseRepository(db),
        storage=storage,
        upload_settings=settings.upload,
    )


def _document_item(item) -> DocumentItem:
    """将服务层传输对象转换为对外 API 数据传输对象。"""
    if isinstance(item, DocumentItem):
        return item
    return DocumentItem(
        id=item.id,
        dataset_id=item.dataset_id,
        name=item.name,
        status=getattr(item, "status", getattr(item, "indexing_status", "")),
        duplicate=getattr(item, "duplicate", False),
    )


def _infrastructure_failure(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            ObjectStorageUnavailable,
            SQLAlchemyError,
            ConnectionError,
            TimeoutError,
            OSError,
        ),
    )


@router.post("/upload", response_model=DocumentUploadResponse, status_code=201)
async def upload_documents(
    dataset_id: str,
    files: list[UploadFile] = File(...),
    service: DocumentService = Depends(get_document_service),
) -> DocumentUploadResponse:
    """依次上传文件，后续文件被拒绝时保留此前已成功上传的文件。"""
    documents: list[DocumentItem] = []
    rejected: list[DocumentRejection] = []

    for file in files:
        try:
            item = await service.upload_one(dataset_id, file, "current-user")
        except UploadValidationError as exc:
            rejected.append(
                DocumentRejection(
                    filename=file.filename or "",
                    code=exc.code,
                    message=exc.message,
                )
            )
            continue
        except DatasetNotFoundError as exc:
            raise HTTPException(status_code=404, detail="knowledge base not found") from exc
        except Exception as exc:
            if _infrastructure_failure(exc):
                # 不记录异常全文/堆栈：SQL 参数、对象名或 SDK 消息可能含敏感内容。
                # 仅输出可用于定位的组件、异常类型和受限格式的存储错误码。
                if isinstance(exc, ObjectStorageUnavailable):
                    component = "storage"
                elif isinstance(exc, SQLAlchemyError):
                    component = "database"
                else:
                    component = "infrastructure"
                cause = exc.__cause__ or exc
                code = getattr(cause, "code", None)
                safe_code = (
                    code
                    if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", code)
                    else "unknown"
                )
                logger.warning(
                    "document_upload_failed component=%s error_type=%s cause_type=%s code=%s",
                    component, type(exc).__name__, type(cause).__name__, safe_code,
                )
                raise HTTPException(
                    status_code=503,
                    detail="document storage is temporarily unavailable",
                ) from exc
            raise
        documents.append(_document_item(item))

    return DocumentUploadResponse(documents=documents, rejected=rejected)


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    dataset_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    q: str | None = Query(None, max_length=255),
    service: DocumentService = Depends(get_document_service),
) -> DocumentListResponse:
    try:
        items, total = await service.list_documents(
            dataset_id,
            page=page,
            page_size=page_size,
            status=status,
            q=q,
        )
    except DatasetNotFoundError as exc:
        raise HTTPException(status_code=404, detail="knowledge base not found") from exc
    except Exception as exc:
        if _infrastructure_failure(exc):
            raise HTTPException(
                status_code=503,
                detail="document storage is temporarily unavailable",
            ) from exc
        raise

    return DocumentListResponse(
        items=[_document_item(item) for item in items],
        total=total,
    )
