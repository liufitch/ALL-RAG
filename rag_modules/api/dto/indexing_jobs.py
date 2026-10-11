from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel, Field

class IndexingJobDocumentResponse(BaseModel):
    id: str
    document_id: str
    status: str
    progress: int = 0
    current_stage: str | None = None
    total_segments: int = 0
    processed_segments: int = 0
    embedded_segments: int = 0
    attempt: int = 0
    error_code: str | None = None
    error: str | None = None

class IndexingJobResponse(BaseModel):
    id: str
    dataset_id: str
    job_type: str
    scope: str
    status: str
    progress: int = 0
    current_stage: str | None = None
    total_documents: int = 0
    processed_documents: int = 0
    completed_documents: int = 0
    failed_documents: int = 0
    error_code: str | None = None
    error: str | None = None
    cancel_requested: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    documents: list[IndexingJobDocumentResponse] = Field(default_factory=list)

class IndexingJobListResponse(BaseModel):
    items: list[IndexingJobResponse]
    total: int

class IndexingJobActionResponse(BaseModel):
    job: IndexingJobResponse
    dispatched: list[str] = Field(default_factory=list)
    dispatch_pending: list[str] = Field(default_factory=list)
