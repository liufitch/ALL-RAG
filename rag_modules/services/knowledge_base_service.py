from __future__ import annotations

from uuid import uuid4

from rag_modules.api.dto.knowledge_base.knowledgeBase import KnowledgeBase
from rag_modules.api.dto.knowledge_base.knowledgeBaseCreate import KnowledgeBaseCreate
from rag_modules.db.models import DatasetRecord
from rag_modules.repositories.knowledge_base_repository import KnowledgeBaseRepository


class KnowledgeBaseService:
    def __init__(self, repository: KnowledgeBaseRepository) -> None:
        self.repository = repository

    async def list_knowledge_bases(
        self,
        page: int,
        page_size: int,
        *,
        status: str = "all",
        visibility: str = "all",
        q: str | None = None,
    ) -> tuple[list[KnowledgeBase], int]:
        rows, total = await self.repository.list(
            page=page,
            page_size=page_size,
            status=status,
            visibility=visibility,
            q=q.strip() if q and q.strip() else None,
        )
        return [self._to_dto(*row) for row in rows], total

    async def knowledge_base_stats(self) -> dict[str, int]:
        return await self.repository.stats()

    async def create_knowledge_base(self, payload: KnowledgeBaseCreate) -> KnowledgeBase:
        knowledge_base_id = uuid4().hex
        record = DatasetRecord(
            id=knowledge_base_id,
            name=payload.name.strip(),
            description=payload.description.strip() or None,
            provider="vendor",
            permission=payload.permission,
            dataset_type=None,
            indexing_technique="high_quality",
            created_by="current-user",
            embedding_model=None,
            embedding_model_provider=None,
            retrieval_model_config=None,
            partial_user_config={"process_rule": None},
        )
        created = await self.repository.create(record)
        return self._to_dto(created, 0, 0, "draft")

    async def get_knowledge_base(self, dataset_id: str) -> KnowledgeBase | None:
        detail = await self.repository.get_active_with_counts(dataset_id)
        if detail is None:
            return None
        return self._to_dto(*detail)

    def _to_dto(
        self, record: DatasetRecord, document_count: int, chunk_count: int, status: str,
    ) -> KnowledgeBase:
        visibility = {
            "private": "private",
            "only_me": "private",
            "team": "team",
            "all_team_members": "team",
            "public": "public",
            "all_members": "public",
        }.get(record.permission, "private")
        permission = {
            "private": "only_me",
            "only_me": "only_me",
            "team": "all_team_members",
            "all_team_members": "all_team_members",
            "public": "all_members",
            "all_members": "all_members",
        }.get(record.permission, "only_me")
        return KnowledgeBase.model_validate(
            {
                "id": record.id,
                "name": record.name,
                "description": record.description or "",
                "permission": permission,
                "indexing_status": {
                    "draft": "not_started", "indexing": "indexing",
                    "ready": "completed", "failed": "failed",
                }[status],
                "category": record.dataset_type or "通用知识",
                "owner": record.created_by,
                "visibility": visibility,
                "embedding_model": record.embedding_model,
                "status": status,
                "document_count": document_count,
                "chunk_count": chunk_count,
                "tags": [],
                "created_at": record.created_at,
                "updated_at": record.updated_at or record.created_at,
            }
        )
