from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["vector", "full_text", "hybrid"] = "hybrid"
    top_k: int = Field(default=5, ge=1, le=100)
    score_threshold: float = Field(default=0.3, ge=0, le=1)
    semantic_weight: float = Field(default=0.7, ge=0, le=1)
    keyword_weight: float = Field(default=0.3, ge=0, le=1)
    rerank_enabled: bool = False

    @model_validator(mode="after")
    def validate_weights(self):
        if abs((self.semantic_weight + self.keyword_weight) - 1) > 1e-6:
            raise ValueError("semantic_weight and keyword_weight must sum to 1")
        return self


class KnowledgeBaseSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    permission: Literal["only_me", "all_team_members", "all_members"] | None = None
    category: str | None = Field(default=None, max_length=255)
    indexing_technique: Literal["high_quality", "economy"] | None = None
    embedding_model: str | None = Field(default=None, max_length=255)
    retrieval: RetrievalConfig | None = None


class KnowledgeBaseSettingsResponse(BaseModel):
    dataset: dict
    indexing: dict
    retrieval: RetrievalConfig
    needs_rebuild: bool = False

