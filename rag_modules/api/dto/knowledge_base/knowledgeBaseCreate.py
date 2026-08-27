from pydantic import BaseModel, Field
from typing import Literal
from rag_modules.api.dto.other.retrievalConfig import RetrievalConfig
from rag_modules.api.dto.other.vectorStoreConfig import VectorStoreConfig
class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    category: str = Field(default="通用知识", max_length=40)
    owner: str = Field(default="当前用户", max_length=40)
    visibility: Literal["private", "team", "public"] = "private"
    embedding_model: str = Field(default="bge-large-zh", max_length=80)
    retrieval_config: RetrievalConfig = Field(default_factory=RetrievalConfig)
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    tags: list[str] = Field(default_factory=list)
