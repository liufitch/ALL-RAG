from __future__ import annotations
import json
import logging
from typing import Annotated
from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, UploadFile
from pydantic import BaseModel, Field
from rag_modules.api.dto.document import DocumentItem, DocumentRejection
from rag_modules.api.dto.process_rule import ProcessRuleConfirmationRequest
from rag_modules.api.file_api import _document_item, _infrastructure_failure, get_document_service
from rag_modules.api.indexing_configuration_api import get_configuration_service
from rag_modules.documents.validation import UploadValidationError
from rag_modules.services.document_service import (
    DatasetNotFoundError,
    DocumentService,
    UploadCompensationCleanupPending,
)
from rag_modules.services.process_rule_service import ProcessRuleService
from rag_modules.tasks.publisher import TaskPublisher, get_task_publisher

router = APIRouter(prefix="/api/knowledge_base/{dataset_id}", tags=["Ingestion"])
logger = logging.getLogger(__name__)

class IngestResponse(BaseModel):
    dataset_id: str
    rule_id: str | None = None
    job_id: str | None = None
    documents: list[DocumentItem] = Field(default_factory=list)
    rejected: list[DocumentRejection] = Field(default_factory=list)
    status: str

@router.post("/ingest", response_model=IngestResponse, status_code=201)
async def ingest_documents(
    dataset_id: Annotated[str, Path(min_length=1, max_length=128)],
    files: list[UploadFile] = File(...),
    config: str = Form(...),
    doc_service: DocumentService = Depends(get_document_service),
    rule_service: ProcessRuleService = Depends(get_configuration_service),
    publisher: TaskPublisher = Depends(get_task_publisher),
) -> IngestResponse:
    try:
        cfg = json.loads(config)
        if not isinstance(cfg, dict):
            raise ValueError("configuration must be an object")
        rule_req = ProcessRuleConfirmationRequest.model_validate({
            **cfg,
            "document_ids": ["ingest-validation"],
            "indexing_technique": cfg.get("indexing_technique", "high_quality"),
            "embedding_model": cfg.get("embedding_model", "bge-m3"),
            "segmentation": cfg.get("segmentation", {"mode": "general"}),
        })
        if rule_req.indexing_technique == "economy" and rule_req.segmentation.mode == "parent_child":
            raise ValueError("parent-child segmentation requires high-quality indexing")
        if rule_req.indexing_technique == "high_quality" and not rule_req.embedding_model:
            raise ValueError("high-quality indexing requires an embedding model")
    except Exception as exc:
        raise HTTPException(422, "Invalid indexing configuration.") from exc

    accepted_items, rejected = [], []
    for f in files:
        try:
            item = await doc_service.upload_one(dataset_id, f, "current-user")
            accepted_items.append(item)
        except UploadValidationError as exc:
            rejected.append(DocumentRejection(filename=f.filename or "", code=exc.code, message=exc.message))
        except DatasetNotFoundError as exc:
            await _compensate_uploads(dataset_id, accepted_items, doc_service, publisher)
            raise HTTPException(404, "knowledge base not found")
        except Exception as exc:
            await _compensate_uploads(dataset_id, accepted_items, doc_service, publisher)
            if _infrastructure_failure(exc):
                raise HTTPException(503, "document storage is temporarily unavailable")
            raise
    if not accepted_items:
        return IngestResponse(dataset_id=dataset_id, documents=[], rejected=rejected, status="rejected")
    rule_req = rule_req.model_copy(update={"document_ids": [item.id for item in accepted_items]})
    try:
        confirmation = await rule_service.confirm(dataset_id, rule_req, actor_id="current-user")
    except Exception:
        await _compensate_uploads(dataset_id, accepted_items, doc_service, publisher)
        raise
    dispatch_pending = False
    for job_doc in confirmation.job_documents:
        try:
            publisher.dispatch_job_document(job_document_id=job_doc.id)
        except Exception:
            dispatch_pending = True
    return IngestResponse(
        dataset_id=dataset_id,
        rule_id=confirmation.rule.id,
        job_id=confirmation.job.id,
        documents=[_document_item(item) for item in accepted_items],
        rejected=rejected,
        status="pending" if dispatch_pending else "queued",
    )


async def _compensate_uploads(dataset_id, items, doc_service, publisher) -> None:
    for item in items:
        if item.duplicate:
            continue
        try:
            await doc_service.compensate_upload(dataset_id, item.id)
        except UploadCompensationCleanupPending:
            try:
                publisher.dispatch_document_cleanup(dataset_id=dataset_id, document_id=item.id)
            except Exception:
                logger.warning("ingest_compensation_pending document_id=%s", item.id)
        except Exception:
            logger.warning("ingest_compensation_failed document_id=%s", item.id)
