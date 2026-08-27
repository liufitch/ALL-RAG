from pydantic  import Field, BaseModel
from typing import Literal
class RetrievalConfig(BaseModel):
    mode: Literal["vector", "full_text", "hybrid"] = "vector"
    top_k: int = Field(default=5, ge=1, le=100)
    score_threshold: float = Field(default=0.3, ge=0, le=1)
    rerank_enabled: bool = False
    rerank_model: str = Field(default="bge-reranker-large", max_length=80)
    semantic_weight: float = Field(default=0.7, ge=0, le=1)
    keyword_weight: float = Field(default=0.3, ge=0, le=1)