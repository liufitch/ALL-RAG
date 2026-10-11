from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Query
from rag_modules.db.session import get_db_session
from rag_modules.repositories.indexing_repository import IndexingRepository
from rag_modules.api.dto.indexing_jobs import *
from rag_modules.tasks.publisher import TaskPublisher, get_task_publisher
from rag_modules.api.dto.process_rule import ProcessRuleConfirmationRequest
from rag_modules.api.indexing_configuration_api import get_configuration_service
from rag_modules.services.process_rule_service import ProcessRuleService, ProcessRuleValidationError

router = APIRouter(prefix="/api/knowledge_base/{dataset_id}/indexing/jobs", tags=["Indexing jobs"])

def _response(job, rows):
    return IndexingJobResponse(
        id=job.id, dataset_id=job.dataset_id, job_type=job.job_type, scope=job.scope,
        status=job.status, progress=job.progress, current_stage=job.current_stage,
        total_documents=job.total_documents, processed_documents=job.processed_documents,
        completed_documents=job.completed_documents, failed_documents=job.failed_documents,
        error_code=job.error_code, error=job.error,
        cancel_requested=job.cancel_requested_at is not None,
        created_at=job.created_at, updated_at=job.updated_at,
        documents=[IndexingJobDocumentResponse(
            id=row.id, document_id=row.document_id, status=row.status, progress=row.progress,
            current_stage=row.current_stage, total_segments=row.total_segments,
            processed_segments=row.processed_segments, embedded_segments=row.embedded_segments,
            attempt=row.attempt, error_code=row.error_code, error=row.error,
        ) for row in rows],
    )

@router.post("", response_model=IndexingJobActionResponse, status_code=202)
async def create_job(
    dataset_id: str, payload: ProcessRuleConfirmationRequest,
    service: ProcessRuleService = Depends(get_configuration_service),
    publisher: TaskPublisher = Depends(get_task_publisher),
):
    try:
        result = await service.confirm(dataset_id, payload, actor_id="current-user")
    except ProcessRuleValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    dispatched, pending = [], []
    for row in result.job_documents:
        try:
            dispatched.append(publisher.dispatch_job_document(job_document_id=row.id) or row.id)
        except Exception:
            pending.append(row.id)
    job, rows = await service.repository.get_job(dataset_id, result.job.id)
    return IndexingJobActionResponse(job=_response(job, rows), dispatched=dispatched, dispatch_pending=pending)

@router.get("", response_model=IndexingJobListResponse)
async def list_jobs(dataset_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), db=Depends(get_db_session)):
    jobs, total = await IndexingRepository(db).list_jobs(dataset_id, offset=(page - 1) * page_size, limit=page_size)
    return IndexingJobListResponse(items=[_response(job, (await IndexingRepository(db).get_job(dataset_id, job.id))[1]) for job in jobs], total=total)

@router.get("/{job_id}", response_model=IndexingJobResponse)
async def get_job(dataset_id: str, job_id: str, db=Depends(get_db_session)):
    job, rows = await IndexingRepository(db).get_job(dataset_id, job_id)
    if job is None:
        raise HTTPException(404, "indexing job not found")
    return _response(job, rows)

@router.post("/{job_id}/cancel", response_model=IndexingJobActionResponse)
async def cancel_job(dataset_id: str, job_id: str, db=Depends(get_db_session)):
    repo = IndexingRepository(db)
    ok, reason = await repo.request_job_cancel(dataset_id, job_id)
    if not ok:
        raise HTTPException(404 if reason == "not_found" else 409, "indexing job is not cancellable" if reason != "not_found" else "indexing job not found")
    job, rows = await repo.get_job(dataset_id, job_id)
    return IndexingJobActionResponse(job=_response(job, rows))

@router.post("/{job_id}/retry", response_model=IndexingJobActionResponse)
async def retry_job(dataset_id: str, job_id: str, db=Depends(get_db_session), publisher: TaskPublisher = Depends(get_task_publisher)):
    repo = IndexingRepository(db)
    ok, reason, identifiers = await repo.retry_job(dataset_id, job_id)
    if not ok:
        status = 404 if reason == "not_found" else 409
        raise HTTPException(status, "indexing job not found" if reason == "not_found" else "indexing job is not retryable")
    dispatched, pending = [], []
    for identifier in identifiers:
        try:
            task_id = publisher.dispatch_job_document(job_document_id=identifier)
            dispatched.append(task_id or identifier)
        except Exception:
            pending.append(identifier)
    job, rows = await repo.get_job(dataset_id, job_id)
    return IndexingJobActionResponse(job=_response(job, rows), dispatched=dispatched, dispatch_pending=pending)
