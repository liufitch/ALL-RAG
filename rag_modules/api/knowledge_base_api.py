from fastapi import APIRouter

from rag_modules.api.dto.knowledge_base.knowledgeBaseListResponse import KnowledgeBaseListResponse

router = APIRouter(prefix="/api/knowledge_base", tags=["知识库管理"])

@APIRouter.get("/list",summary="知识库列表" ,response_model=KnowledgeBaseListResponse)
def list()->KnowledgeBaseListResponse:
    return {}
