from pydantic import BaseModel, Field

class KnowledgeBaseStats(BaseModel):
    total: int
    ready: int
    indexing: int
    draft: int
    documents: int
    chunks: int