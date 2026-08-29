from pydantic import BaseModel, Field
from typing import Literal

class VectorStoreConfig(BaseModel):
    provider: Literal["milvus", "pgvector", "qdrant", "weaviate", "opensearch", "elasticsearch"] = "milvus"
    collection_name: str = Field(default="", max_length=255)
    embedding_dimension: int = Field(default=1024, ge=2, le=32768)
    metric_type: Literal["COSINE", "IP", "L2"] = "COSINE"
    auto_create_collection: bool = True