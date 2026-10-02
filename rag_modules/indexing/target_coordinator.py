"""Coordinate vector dimension discovery and Milvus collection creation."""

from __future__ import annotations

from rag_modules.common import utcnow
from rag_modules.indexing.models import VectorTarget


class IndexTargetCoordinator:
    def __init__(self, session, vector_store):
        self.session = session
        self.vector_store = vector_store

    async def resolve(self, index_id: str, discovered_dimension: int) -> VectorTarget:
        from rag_modules.db.models import DatasetIndexRecord

        index = await self.session.get(DatasetIndexRecord, index_id, with_for_update=True)
        if index is None:
            raise ValueError("index target not found")
        if index.embedding_dimension is not None and index.embedding_dimension != discovered_dimension:
            from rag_modules.indexing.engine import DocumentIndexingError

            raise DocumentIndexingError("EMBEDDING_DIMENSION_MISMATCH", False, "Embedding dimension does not match the index.")
        index.embedding_dimension = discovered_dimension
        index.collection_name = index.collection_name or f"graph_rag_{index.dataset_id}_{index.id}"
        index.updated_at = utcnow()
        await self.session.commit()
        self.vector_store.ensure_collection(index.collection_name, discovered_dimension, index.distance_metric or "COSINE")
        return VectorTarget(index.collection_name, discovered_dimension)
