from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.exc import SQLAlchemyError

from rag_modules.api.dto.process_rule import ProcessRuleConfirmationRequest, ProcessRuleResponse
from rag_modules.db.session import get_db_session
from rag_modules.repositories.process_rule_repository import ProcessRuleRepository
from rag_modules.services.process_rule_service import ProcessRuleService, ProcessRuleValidationError
from rag_modules.tasks.publisher import TaskPublisher, get_task_publisher

router = APIRouter(prefix="/api/knowledge_base/{dataset_id}/indexing", tags=["Indexing"])


def get_configuration_service(db=Depends(get_db_session)) -> ProcessRuleService:
    return ProcessRuleService(ProcessRuleRepository(db))


@router.post("/configure", response_model=ProcessRuleResponse, status_code=202)
async def configure_indexing(
    dataset_id: Annotated[str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")],
    request: ProcessRuleConfirmationRequest,
    service: ProcessRuleService = Depends(get_configuration_service),
    publisher: TaskPublisher = Depends(get_task_publisher),
) -> ProcessRuleResponse:
    try:
        result = await service.confirm(dataset_id, request, actor_id="current-user")
    except ProcessRuleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="index configuration is temporarily unavailable") from exc

    dispatch_pending = False
    for job_document in result.job_documents:
        try:
            publisher.dispatch_job_document(job_document_id=job_document.id)
        except Exception:
            dispatch_pending = True
    return ProcessRuleResponse(
        rule_id=result.rule.id,
        job_id=result.job.id,
        document_ids=[item.document_id for item in result.job_documents],
        status="pending" if dispatch_pending else "queued",
    )
