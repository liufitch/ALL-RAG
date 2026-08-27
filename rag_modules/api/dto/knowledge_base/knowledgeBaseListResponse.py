from pydantic import BaseModel
from rag_modules.api.dto.knowledge_base.knowledgeBase import KnowledgeBase


class KnowledgeBaseListResponse(BaseModel):
    items: list[KnowledgeBase]
    total: int