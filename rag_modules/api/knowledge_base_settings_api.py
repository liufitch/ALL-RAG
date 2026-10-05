from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.background import BackgroundTasks
from fastapi.responses import FileResponse
from sqlalchemy.exc import SQLAlchemyError

from rag_modules.api.dto.knowledge_base.settings import KnowledgeBaseSettingsUpdate
from rag_modules.db.session import get_db_session
from rag_modules.object_storage import ObjectStorage, ObjectStorageUnavailable
from rag_modules.object_storage.factory import get_object_storage
from rag_modules.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag_modules.services.knowledge_base_archive_service import ArchiveValidationError, KnowledgeBaseArchiveService
from rag_modules.services.knowledge_base_service import KnowledgeBaseService
from rag_modules.tasks.publisher import TaskPublisher, get_task_publisher

router = APIRouter(prefix="/api/knowledge_base", tags=["知识库设置"])


def get_knowledge_base_settings_service(db=Depends(get_db_session)):
    return KnowledgeBaseService(KnowledgeBaseRepository(db))


def get_archive_service(db=Depends(get_db_session), storage: ObjectStorage = Depends(get_object_storage)):
    return KnowledgeBaseArchiveService(KnowledgeBaseRepository(db), storage)


@router.get("/{dataset_id}/settings")
async def get_settings(dataset_id: str, service: KnowledgeBaseService = Depends(get_knowledge_base_settings_service)):
    result = await service.get_settings(dataset_id)
    if result is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return result


@router.patch("/{dataset_id}/settings")
async def update_settings(
    dataset_id: str,
    payload: KnowledgeBaseSettingsUpdate,
    service: KnowledgeBaseService = Depends(get_knowledge_base_settings_service),
):
    result = await service.update_settings(dataset_id, payload, "current-user")
    if result is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return result


@router.post("/{dataset_id}/rebuild", status_code=202)
async def rebuild(
    dataset_id: str,
    service: KnowledgeBaseService = Depends(get_knowledge_base_settings_service),
    publisher: TaskPublisher = Depends(get_task_publisher),
):
    result = await service.rebuild(dataset_id, "current-user")
    if result is None:
        raise HTTPException(status_code=404, detail="knowledge base not found or has no documents")
    pending = False
    for job_document_id in result.get("job_document_ids", []):
        try:
            publisher.dispatch_job_document(job_document_id=job_document_id)
        except Exception:
            pending = True
    return {"job_id": result["job_id"], "status": "pending" if pending else result["status"]}


@router.get("/{dataset_id}/export")
async def export_dataset(
    dataset_id: str,
    background_tasks: BackgroundTasks,
    service: KnowledgeBaseArchiveService = Depends(get_archive_service),
):
    try:
        filename = await service.export_dataset(dataset_id)
    except ArchiveValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ObjectStorageUnavailable, SQLAlchemyError) as exc:
        raise HTTPException(status_code=503, detail="knowledge base export is temporarily unavailable") from exc
    background_tasks.add_task(_remove_file, filename)
    return FileResponse(filename, media_type="application/zip", filename=f"knowledge-base-{dataset_id}.zip")


@router.post("/import", status_code=201)
async def import_dataset(
    file: UploadFile = File(...),
    rebuild: bool = Query(False),
    archive_service: KnowledgeBaseArchiveService = Depends(get_archive_service),
    settings_service: KnowledgeBaseService = Depends(get_knowledge_base_settings_service),
    publisher: TaskPublisher = Depends(get_task_publisher),
):
    try:
        result = await archive_service.import_dataset(file.file)
    except ArchiveValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ObjectStorageUnavailable, SQLAlchemyError) as exc:
        raise HTTPException(status_code=503, detail="knowledge base import is temporarily unavailable") from exc
    if rebuild:
        job = await settings_service.rebuild(result["dataset_id"], "current-user")
        pending = False
        if job:
            for job_document_id in job.get("job_document_ids", []):
                try:
                    publisher.dispatch_job_document(job_document_id=job_document_id)
                except Exception:
                    pending = True
            result.update({"job_id": job["job_id"], "rebuild_status": "pending" if pending else "queued"})
    return result


def _remove_file(filename: str) -> None:
    import os
    try:
        os.unlink(filename)
    except OSError:
        pass
