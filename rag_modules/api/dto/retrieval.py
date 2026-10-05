from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    mode: Literal["vector", "full_text", "hybrid"] | None = None
    top_k: int | None = Field(default=None, ge=1, le=100)
    score_threshold: float | None = Field(default=None, ge=0, le=1)
    semantic_weight: float | None = Field(default=None, ge=0, le=1)
    keyword_weight: float | None = Field(default=None, ge=0, le=1)
    rerank_enabled: bool | None = None


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
