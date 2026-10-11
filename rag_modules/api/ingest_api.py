from __future__ import annotations
import json
from typing import Annotated
from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, UploadFile
from pydantic import BaseModel, Field
from rag_modules.api.dto.document import DocumentItem, DocumentRejection
from rag_modules.api.dto.indexing_preview import GeneralSegmentationRequest, ParentChildSegmentationRequest
from rag_modules.api.dto.process_rule import ProcessRuleConfirmationRequest
from rag_modules.api.file_api import _document_item, _infrastructure_failure, get_document_service
from rag_modules.api.indexing_configuration_api import get_configuration_service
from rag_modules.documents.validation import UploadValidationError
from rag_modules.services.document_service import DatasetNotFoundError, DocumentService
from rag_modules.services.process_rule_service import ProcessRuleService
from rag_modules.tasks.publisher import TaskPublisher, get_task_publisher

router = APIRouter(prefix="/api/knowledge_base/{dataset_id}", tags=["Ingestion"])

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
    except Exception as exc:
        raise HTTPException(422, f"Invalid config JSON: {exc}") from exc
    accepted_items, rejected = [], []
    for f in files:
        try:
            item = await doc_service.upload_one(dataset_id, f, "current-user")
            accepted_items.append(item)
        except UploadValidationError as exc:
            rejected.append(DocumentRejection(filename=f.filename or "", code=exc.code, message=exc.message))
        except DatasetNotFoundError:
            raise HTTPException(404, "knowledge base not found")
        except Exception as exc:
            if _infrastructure_failure(exc):
                raise HTTPException(503, "document storage is temporarily unavailable")
            raise
    if not accepted_items:
        return IngestResponse(dataset_id=dataset_id, documents=[], rejected=rejected, status="rejected")
    doc_ids = [item.id for item in accepted_items]
    seg_raw = cfg.get("segmentation", {})
    mode = seg_raw.get("mode", "general")
    seg_model = ParentChildSegmentationRequest(**seg_raw) if mode == "parent_child" else GeneralSegmentationRequest(**seg_raw)
    rule_req = ProcessRuleConfirmationRequest(
        document_ids=doc_ids,
        indexing_technique=cfg.get("indexing_technique", "high_quality"),
        embedding_model=cfg.get("embedding_model", "bge-m3"),
        segmentation=seg_model,
    )
    confirmation = await rule_service.confirm(dataset_id, rule_req, actor_id="current-user")
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
