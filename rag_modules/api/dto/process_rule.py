from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from rag_modules.api.dto.indexing_preview import IndexingPreviewRequest


class ProcessRuleConfirmationRequest(IndexingPreviewRequest):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProcessRuleResponse(BaseModel):
    rule_id: str
    job_id: str
    document_ids: list[str] = Field(default_factory=list)
    status: str

