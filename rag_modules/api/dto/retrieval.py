from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    mode: Literal["vector", "full_text", "hybrid"] = "hybrid"
    top_k: int = Field(default=5, ge=1, le=100)
    score_threshold: float = Field(default=0.3, ge=0, le=1)
    semantic_weight: float = Field(default=0.7, ge=0, le=1)
    keyword_weight: float = Field(default=0.3, ge=0, le=1)
    rerank_enabled: bool = False


class RetrievalItemResponse(BaseModel):
    segment_id: str
    content: str
    score: float
    match_type: str
    document_id: str
    document_name: str
    source_metadata: dict[str, Any] | None = None
    parent_id: str | None = None


class RetrievalResponse(BaseModel):
    items: list[RetrievalItemResponse]
    rerank_trace: list[dict[str, Any]]
