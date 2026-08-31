from datetime import datetime

from pydantic import BaseModel, Field
from typing import Literal


class KnowledgeBase(BaseModel):
    id: str
    name: str
    description: str = ""
    permission: Literal["only_me", "all_team_members", "all_members"] = "only_me"
    indexing_status: Literal["not_started", "indexing", "completed", "failed"] = "not_started"
    category: str = "通用知识"
    owner: str = "当前用户"
    visibility: Literal["private", "team", "public"] = "private"
    embedding_model: str | None = None
    status: Literal["draft", "indexing", "ready", "failed"] = "draft"
    document_count: int = 0
    chunk_count: int = 0
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
