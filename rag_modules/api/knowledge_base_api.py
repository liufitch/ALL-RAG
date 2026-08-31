from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from rag_modules.api.dto.knowledge_base.knowledgeBase import KnowledgeBase
from rag_modules.api.dto.knowledge_base.knowledgeBaseCreate import KnowledgeBaseCreate
from rag_modules.api.dto.knowledge_base.knowledgeBaseListResponse import KnowledgeBaseListResponse
from rag_modules.db.session import get_db_session
from rag_modules.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag_modules.services.knowledge_base_service import KnowledgeBaseService

router = APIRouter(prefix="/api/knowledge_base", tags=["知识库管理"])


def get_knowledge_base_service(db=Depends(get_db_session)):
    return KnowledgeBaseService(KnowledgeBaseRepository(db))


@router.get("/list", summary="查询知识库列表", response_model=KnowledgeBaseListResponse)
async def list_knowledge_base(
    service: KnowledgeBaseService = Depends(get_knowledge_base_service),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
) -> KnowledgeBaseListResponse:
    items, total = await service.list_knowledge_bases(page=page, page_size=page_size)
    return KnowledgeBaseListResponse(items=items, total=total)


@router.get("/stats", summary="知识库统计")
async def knowledge_base_stats(
    service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> dict[str, int]:
    return await service.knowledge_base_stats()


@router.post("", summary="创建知识库", response_model=KnowledgeBase)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> KnowledgeBase:
    return await service.create_knowledge_base(payload)


@router.delete("/{knowledge_base_id}", summary="删除知识库", status_code=204)
async def delete_knowledge_base(
    knowledge_base_id: str,
    db=Depends(get_db_session),
) -> None:
    deleted = await KnowledgeBaseRepository(db).soft_delete(knowledge_base_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="knowledge base not found")
