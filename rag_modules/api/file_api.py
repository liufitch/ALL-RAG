from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from rag_modules.api.dto.document import (
    DocumentItem,
    DocumentListResponse,
    DocumentActionResponse,
    DocumentRenameRequest,
    DocumentRevisionResponse,
    DocumentJobResponse,
    DocumentSegmentItem,
    DocumentSegmentListResponse,
    DocumentSegmentUpdate,
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
from rag_modules.services.document_service import (
    DatasetNotFoundError,
    DocumentNotFoundError,
    DocumentService,
    DocumentValidationError,
)
from rag_modules.services.document_revision_service import (
    DocumentRevisionService,
    RevisionConflictError,
)
from rag_modules.repositories.indexing_repository import IndexingRepository
from rag_modules.db.models import IndexingJobDocumentRecord, IndexingJobRecord
from sqlalchemy import select
from rag_modules.tasks.publisher import TaskPublisher, get_task_publisher

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
        enabled=getattr(item, "enabled", True),
        archived=getattr(item, "archived", False),
        updated_at=getattr(item, "updated_at", None),
        error=getattr(item, "error", None),
        size=(getattr(item, "data_source_info", None) or {}).get("size"),
        content_type=(getattr(item, "data_source_info", None) or {}).get("content_type"),
        segment_count=getattr(item, "segment_count", None),
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


async def _upload_documents(
    dataset_id: str,
    files: list[UploadFile],
    service: DocumentService,
    publisher: TaskPublisher,
) -> DocumentUploadResponse:
    """执行批量上传；索引必须在用户确认处理规则后投递。"""
    documents: list[DocumentItem] = []
    rejected: list[DocumentRejection] = []
    indexing_task_ids: list[str] = []
    indexing_dispatch_pending: list[str] = []

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
                component = "storage" if isinstance(exc, ObjectStorageUnavailable) else "database" if isinstance(exc, SQLAlchemyError) else "infrastructure"
                cause = exc.__cause__ or exc
                code = getattr(cause, "code", None)
                safe_code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", code) else "unknown"
                logger.warning(
                    "document_upload_failed component=%s error_type=%s cause_type=%s code=%s",
                    component, type(exc).__name__, type(cause).__name__, safe_code,
                )
                raise HTTPException(status_code=503, detail="document storage is temporarily unavailable") from exc
            raise

        document = _document_item(item)
        documents.append(document)
        # 重复文件没有新对象和新业务记录，不能重复创建索引任务。
        if document.duplicate:
            continue
        # 上传只保存原始文件和 waiting 文档。处理规则尚未确认前，
        # 不创建兼容索引任务，也不向 RabbitMQ 投递消息；确认接口
        # 会在规则和任务事务提交后再投递 job-document 消息。

    return DocumentUploadResponse(
        documents=documents,
        rejected=rejected,
        indexing_task_ids=indexing_task_ids,
        indexing_dispatch_pending=indexing_dispatch_pending,
    )


async def upload_documents(
    dataset_id: str,
    files: list[UploadFile] = File(...),
    service: DocumentService = Depends(get_document_service),
    publisher: TaskPublisher | None = None,
) -> DocumentUploadResponse:
    """上传业务函数；保留可直接调用形式，便于服务层/API 回归测试复用。"""
    # 直接调用时没有 FastAPI 依赖注入，因此不主动连接 broker；HTTP 包装器
    # 会显式传入发布器。这样测试上传补偿逻辑时不会依赖 RabbitMQ。
    if publisher is None:
        publisher = _NoopTaskPublisher()
    return await _upload_documents(dataset_id, files, service, publisher)


class _NoopTaskPublisher(TaskPublisher):
    """仅用于直接调用业务函数时保持旧测试的本地、无 broker 语义。"""

    def __init__(self) -> None:
        pass

    def dispatch_document(self, *, dataset_id: str, document_id: str) -> str | None:
        return None


@router.post("", response_model=DocumentUploadResponse, status_code=201)
@router.post("/upload", response_model=DocumentUploadResponse, status_code=201)
async def upload_documents_endpoint(
    dataset_id: str,
    files: list[UploadFile] = File(...),
    service: DocumentService = Depends(get_document_service),
    publisher: TaskPublisher = Depends(get_task_publisher),
) -> DocumentUploadResponse:
    """HTTP 入口：通过依赖注入获取发布器并触发异步索引投递。"""
    return await upload_documents(dataset_id, files, service, publisher)


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


def _document_segment_item(segment) -> DocumentSegmentItem:
    return DocumentSegmentItem(
        id=segment.id,
        position=segment.position,
        content=segment.content,
        question=segment.question,
        answer=segment.answer,
        keywords=list(segment.keywords or []),
        parent_id=segment.parent_id,
        index_type=segment.index_type,
        source_metadata=dict(segment.source_metadata or {}),
    )


async def _governance_document(dataset_id: str, document_id: str, service: DocumentService):
    try:
        return await service.get_document(dataset_id, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc


@router.get("/{document_id}/download")
async def download_document(
    dataset_id: str,
    document_id: str,
    service: DocumentService = Depends(get_document_service),
):
    document = await _governance_document(dataset_id, document_id, service)
    info = document.data_source_info or {}
    object_key = info.get("object_key")
    if not object_key:
        raise HTTPException(status_code=404, detail="document source not found")

    async def stream():
        try:
            async with service.storage.get_stream(object_key) as source:
                while True:
                    chunk = await __import__("anyio").to_thread.run_sync(source.read, 1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        except ObjectStorageUnavailable as exc:
            logger.warning("document_download_failed document_id=%s", document_id)
            raise HTTPException(status_code=503, detail="document storage is temporarily unavailable") from exc

    filename = document.name.replace('"', "'").replace("\r", " ").replace("\n", " ")
    media_type = info.get("content_type") or "application/octet-stream"
    return StreamingResponse(
        stream(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/{document_id}", response_model=DocumentActionResponse)
async def rename_document(
    dataset_id: str,
    document_id: str,
    request: DocumentRenameRequest,
    service: DocumentService = Depends(get_document_service),
):
    try:
        document = await service.rename_document(dataset_id, document_id, request.name, "current-user")
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except DocumentValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return DocumentActionResponse(document=_document_item(document))


async def _set_document_enabled(dataset_id: str, document_id: str, enabled: bool, service: DocumentService):
    try:
        document = await service.set_enabled(dataset_id, document_id, enabled, "current-user")
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    return DocumentActionResponse(document=_document_item(document))


@router.post("/{document_id}/enable", response_model=DocumentActionResponse)
async def enable_document(dataset_id: str, document_id: str, service: DocumentService = Depends(get_document_service)):
    return await _set_document_enabled(dataset_id, document_id, True, service)


@router.post("/{document_id}/disable", response_model=DocumentActionResponse)
async def disable_document(dataset_id: str, document_id: str, service: DocumentService = Depends(get_document_service)):
    return await _set_document_enabled(dataset_id, document_id, False, service)


async def _set_document_archived(dataset_id: str, document_id: str, archived: bool, service: DocumentService):
    try:
        document = await service.set_archived(dataset_id, document_id, archived, "current-user")
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    return DocumentActionResponse(document=_document_item(document))


@router.post("/{document_id}/archive", response_model=DocumentActionResponse)
async def archive_document(dataset_id: str, document_id: str, service: DocumentService = Depends(get_document_service)):
    return await _set_document_archived(dataset_id, document_id, True, service)


@router.post("/{document_id}/restore", response_model=DocumentActionResponse)
async def restore_document(dataset_id: str, document_id: str, service: DocumentService = Depends(get_document_service)):
    return await _set_document_archived(dataset_id, document_id, False, service)


@router.delete("/{document_id}", response_model=DocumentActionResponse)
async def delete_document(
    dataset_id: str,
    document_id: str,
    service: DocumentService = Depends(get_document_service),
    publisher: TaskPublisher = Depends(get_task_publisher),
):
    try:
        document = await service.delete_document(dataset_id, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    try:
        publisher.dispatch_document_cleanup(dataset_id=dataset_id, document_id=document_id)
    except Exception:
        logger.warning("document_cleanup_dispatch_pending document_id=%s", document_id)
    return DocumentActionResponse(document=_document_item(document))


@router.get("/{document_id}/segments", response_model=DocumentSegmentListResponse)
async def list_document_segments(dataset_id: str, document_id: str, service: DocumentService = Depends(get_document_service)):
    try:
        segments = await service.list_segments(dataset_id, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    return DocumentSegmentListResponse(
        document_id=document_id,
        items=[_document_segment_item(segment) for segment in segments],
    )


@router.post("/{document_id}/reindex", response_model=DocumentActionResponse, status_code=202)
async def reindex_document(
    dataset_id: str,
    document_id: str,
    service: DocumentService = Depends(get_document_service),
    db=Depends(get_db_session),
    publisher: TaskPublisher = Depends(get_task_publisher),
):
    document = await _governance_document(dataset_id, document_id, service)
    job_document_id = await IndexingRepository(db).ensure_compatibility_job_document(dataset_id, document_id)
    job_id = None
    if job_document_id:
        job_document = await db.get(IndexingJobDocumentRecord, job_document_id)
        job_id = job_document.job_id if job_document else None
        try:
            publisher.dispatch_job_document(job_document_id=job_document_id)
        except Exception:
            logger.warning("document_reindex_dispatch_pending document_id=%s", document_id)
    return DocumentActionResponse(document=_document_item(document), job_id=job_id)


@router.patch("/{document_id}/segments", response_model=DocumentRevisionResponse, status_code=202)
async def update_document_segments(
    dataset_id: str,
    document_id: str,
    request: DocumentSegmentUpdate,
    db=Depends(get_db_session),
    publisher: TaskPublisher = Depends(get_task_publisher),
):
    try:
        result = await DocumentRevisionService(db).create_revision_and_job(
            dataset_id, document_id, request, "current-user"
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except RevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        publisher.dispatch_job_document(job_document_id=result.job_document_id)
    except Exception:
        logger.warning("document_revision_dispatch_pending document_id=%s", document_id)
    return DocumentRevisionResponse(
        revision_id=result.revision.id,
        version=result.revision.version,
        job_id=result.job.id,
    )


@router.get("/{document_id}/jobs/{job_id}", response_model=DocumentJobResponse)
async def get_document_job(
    dataset_id: str,
    document_id: str,
    job_id: str,
    db=Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
):
    await _governance_document(dataset_id, document_id, service)
    job = await db.scalar(
        select(IndexingJobRecord).where(
            IndexingJobRecord.id == job_id,
            IndexingJobRecord.dataset_id == dataset_id,
            IndexingJobRecord.revision_id.is_not(None),
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="indexing job not found")
    return DocumentJobResponse(
        id=job.id,
        status=job.status,
        progress=int(job.progress or 0),
        current_stage=job.current_stage,
        error=job.error,
    )
