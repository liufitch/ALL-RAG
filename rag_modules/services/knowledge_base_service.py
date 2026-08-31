from __future__ import annotations

from uuid import uuid4

from rag_modules.api.dto.knowledge_base.knowledgeBase import KnowledgeBase
from rag_modules.api.dto.knowledge_base.knowledgeBaseCreate import KnowledgeBaseCreate
from rag_modules.api.dto.other.vectorStoreConfig import VectorStoreConfig
from rag_modules.common import safe_collection_name
from rag_modules.config.settings import settings
from rag_modules.db.models import DatasetRecord
from rag_modules.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag_modules.vector_stores.factory import get_vector_store

DEFAULT_EMBEDDING_DIMENSIONS = {
    "bge-large-zh": 1024,
    "bge-m3": 1024,
    "text-embedding-3-large": 3072,
}


class KnowledgeBaseService:
    def __init__(self, repository: KnowledgeBaseRepository) -> None:
        self.repository = repository

    async def list_knowledge_bases(self, page: int, page_size: int) -> tuple[list[KnowledgeBase], int]:
        rows, total = await self.repository.list(page=page, page_size=page_size)
        return [self._to_dto(record, document_count, chunk_count) for record, document_count, chunk_count in rows], total

    async def knowledge_base_stats(self) -> dict[str, int]:
        items, total = await self.list_knowledge_bases(page=1, page_size=10_000)
        return {
            "total": total,
            "ready": sum(1 for item in items if item.status == "ready"),
            "indexing": sum(1 for item in items if item.status == "indexing"),
            "draft": sum(1 for item in items if item.status == "draft"),
            "documents": sum(item.document_count for item in items),
            "chunks": sum(item.chunk_count for item in items),
        }

    async def create_knowledge_base(self, payload: KnowledgeBaseCreate) -> KnowledgeBase:
        knowledge_base_id = uuid4().hex
        vector_store = self._normalize_vector_store(
            knowledge_base_id=knowledge_base_id,
            embedding_model=payload.embedding_model,
            vector_store=payload.vector_store,
        )

        provider = get_vector_store(vector_store.provider)
        provider.provision_collection(
            collection_name=vector_store.collection_name,
            dimension=vector_store.embedding_dimension,
            metric_type=vector_store.metric_type,
        )

        record = DatasetRecord(
            id=knowledge_base_id,
            name=payload.name.strip(),
            description=payload.description.strip() or None,
            provider=vector_store.provider,
            permission=payload.visibility,
            dataset_type=payload.category.strip() or None,
            indexing_technique=payload.retrieval_config.mode,
            created_by=payload.owner.strip()[:36] or "current-user",
            embedding_model=payload.embedding_model.strip() or None,
            embedding_model_provider=None,
            retrieval_model_config=payload.retrieval_config.model_dump(mode="python"),
            partial_user_config=vector_store.model_dump(mode="python"),
        )
        created = await self.repository.create(record)
        return self._to_dto(created, 0, 0)

    def _normalize_vector_store(
        self,
        knowledge_base_id: str,
        embedding_model: str,
        vector_store: VectorStoreConfig,
    ) -> VectorStoreConfig:
        collection_name = vector_store.collection_name.strip()
        if not collection_name:
            collection_name = safe_collection_name(
                f"{settings.vector_store.collection_prefix}_{knowledge_base_id}"
            )
        dimension = vector_store.embedding_dimension
        if dimension <= 0:
            dimension = DEFAULT_EMBEDDING_DIMENSIONS.get(embedding_model, 1024)
        return vector_store.model_copy(
            update={
                "collection_name": safe_collection_name(collection_name),
                "embedding_dimension": dimension,
            }
        )

    def _to_dto(self, record: DatasetRecord, document_count: int, chunk_count: int) -> KnowledgeBase:
        permission = {
            "private": "private",
            "only_me": "private",
            "team": "team",
            "all_team_members": "team",
            "public": "public",
            "all_members": "public",
        }.get(record.permission, "private")
        partial_user_config = record.partial_user_config or {}
        if not isinstance(partial_user_config, dict):
            partial_user_config = {}
        provider = partial_user_config.get("provider", "milvus")
        if provider not in {"milvus", "pgvector", "qdrant", "weaviate", "opensearch", "elasticsearch"}:
            provider = "milvus"
        vector_store = {
            "provider": provider,
            **partial_user_config,
        }
        vector_store["provider"] = provider
        return KnowledgeBase.model_validate(
            {
                "id": record.id,
                "name": record.name,
                "description": record.description or "",
                "category": record.dataset_type or "通用知识",
                "owner": record.created_by,
                "visibility": permission,
                "embedding_model": record.embedding_model or "bge-large-zh",
                "retrieval_config": record.retrieval_model_config or {},
                "vector_store": vector_store,
                "status": "ready" if record.deleted_at is None else "failed",
                "document_count": document_count,
                "chunk_count": chunk_count,
                "tags": [],
                "created_at": record.created_at,
                "updated_at": record.updated_at or record.created_at,
            }
        )
