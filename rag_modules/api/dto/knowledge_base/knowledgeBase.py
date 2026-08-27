from pydantic import BaseModel, Field
from typing import Literal
from rag_modules.api.dto.other.retrievalConfig import RetrievalConfig
from rag_modules.api.dto.other.vectorStoreConfig import VectorStoreConfig
from datetime import datetime
class KnowledgeBase(BaseModel):
    id: str
    name: str
    description: str = ""
    category: str = "通用知识"
    owner: str = "当前用户"
    visibility: Literal["private", "team", "public"] = "private"
    embedding_model: str = "bge-large-zh"
    retrieval_config: RetrievalConfig = Field(default_factory=RetrievalConfig)
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    status: Literal["draft", "indexing", "ready", "failed"] = "draft"
    document_count: int = 0
    chunk_count: int = 0
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime