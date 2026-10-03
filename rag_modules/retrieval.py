from __future__ import annotations

from dataclasses import dataclass
import inspect
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_modules.db.models import DatasetIndexRecord, DatasetRecord, DocumentRecord, DocumentSegmentRecord
from rag_modules.indexing.keywords import KeywordExtractor
from rag_modules.vector_stores.base import VectorSearchHit


@dataclass(frozen=True, slots=True)
class RetrievalItem:
    segment_id: str
    content: str
    score: float
    match_type: str
    document_id: str
    document_name: str
    source_metadata: dict[str, Any] | None
    parent_id: str | None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    items: list[RetrievalItem]
    rerank_trace: list[dict[str, Any]]


class RetrievalService:
    def __init__(self, session: AsyncSession, *, vector_store: Any = None,
                 embedder: Any = None, reranker: Any = None,
                 keyword_extractor: KeywordExtractor | None = None) -> None:
        self.session = session
        self.vector_store = vector_store
        self.embedder = embedder
        self.reranker = reranker
        self.keyword_extractor = keyword_extractor or KeywordExtractor()

    async def retrieve(self, dataset_id: str, query: str, *, mode: str = "vector",
                       top_k: int = 5, score_threshold: float = 0.3,
                       semantic_weight: float = 0.7, keyword_weight: float = 0.3,
                       rerank_enabled: bool = False) -> RetrievalResult:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be empty")
        if mode not in {"vector", "full_text", "hybrid"}:
            raise ValueError("unsupported retrieval mode")
        index_result = await self.session.execute(select(DatasetIndexRecord).where(
            DatasetIndexRecord.dataset_id == dataset_id,
            DatasetIndexRecord.status == "active",
            DatasetIndexRecord.deleted_at.is_(None),
        ))
        index = index_result.scalar_one_or_none()
        if index is None:
            return RetrievalResult([], [])
        tokens = self.keyword_extractor.extract(query, limit=100)
        vector_hits: list[VectorSearchHit] = []
        if mode in {"vector", "hybrid"} and self.vector_store is not None and index.collection_name:
            vector = await self._embed(query, index.embedding_model)
            vector_hits = self.vector_store.search(
                index.collection_name, vector, dataset_id, index.id, max(top_k * 4, top_k)
            )
        keyword_scores: dict[str, float] = {}
        if mode in {"full_text", "hybrid"} and tokens:
            rows = await self._keyword_rows(dataset_id, index.id, tokens)
            for row in rows:
                terms = set(row.keywords or [])
                keyword_scores[row.id] = len(terms.intersection(tokens)) / max(1, len(tokens))
        semantic_scores = {hit.id: hit.score for hit in vector_hits}
        ids = set(semantic_scores) | set(keyword_scores)
        if not ids:
            return RetrievalResult([], [])
        rows_result = await self.session.execute(select(DocumentSegmentRecord, DocumentRecord).join(
            DocumentRecord, DocumentRecord.id == DocumentSegmentRecord.document_id
        ).where(
            DocumentSegmentRecord.id.in_(ids),
            DocumentSegmentRecord.dataset_id == dataset_id,
            DocumentSegmentRecord.dataset_index_id == index.id,
            DocumentSegmentRecord.deleted_at.is_(None),
            DocumentSegmentRecord.status == "completed",
            DocumentRecord.deleted_at.is_(None),
            DocumentRecord.enabled.is_(True),
            DocumentRecord.archived.is_(False),
        ))
        rows = {segment.id: (segment, document) for segment, document in rows_result.all()}
        parent_ids = {segment.parent_id for segment, _ in rows.values() if segment.parent_id}
        parents: dict[str, DocumentSegmentRecord] = {}
        if parent_ids:
            parent_result = await self.session.execute(select(DocumentSegmentRecord).where(
                DocumentSegmentRecord.id.in_(parent_ids),
                DocumentSegmentRecord.deleted_at.is_(None),
            ))
            parents = {parent.id: parent for parent in parent_result.scalars()}
            for identifier, (segment, document) in list(rows.items()):
                if segment.parent_id in parents:
                    parent = parents[segment.parent_id]
                    rows[identifier] = (segment, document)
        max_semantic = max(semantic_scores.values(), default=1.0) or 1.0
        max_keyword = max(keyword_scores.values(), default=1.0) or 1.0
        candidates: list[tuple[float, str, str]] = []
        for identifier in ids:
            if identifier not in rows:
                continue
            semantic = semantic_scores.get(identifier, 0.0)
            if mode == "hybrid":
                semantic /= max_semantic
            keyword = keyword_scores.get(identifier, 0.0) / max_keyword
            if mode == "vector":
                score, match = semantic, "vector"
            elif mode == "full_text":
                score, match = keyword, "keyword"
            else:
                score = semantic_weight * semantic + keyword_weight * keyword
                match = "hybrid" if semantic and keyword else ("vector" if semantic else "keyword")
            if score >= score_threshold:
                candidates.append((score, identifier, match))
        candidates.sort(key=lambda value: (-value[0], value[1]))
        trace = [{"segment_id": identifier, "before_rank": rank + 1, "after_rank": rank + 1, "score": score}
                 for rank, (score, identifier, _) in enumerate(candidates)]
        if rerank_enabled and self.reranker is not None and candidates:
            try:
                reranked = self.reranker(query, [rows[item_id][0].content for _, item_id, _ in candidates])
                if inspect.isawaitable(reranked):
                    reranked = await reranked
                if isinstance(reranked, Sequence) and len(reranked) == len(candidates):
                    candidates = [item for _, item in sorted(zip(reranked, candidates), key=lambda pair: -float(pair[0]))]
                    for rank, (_, identifier, _) in enumerate(candidates):
                        trace[next(i for i, item in enumerate(trace) if item["segment_id"] == identifier)]["after_rank"] = rank + 1
            except (RuntimeError, TypeError, ValueError, OSError):
                # Reranking is an optimization; a provider outage must not hide base recall.
                pass
        items = [self._item(rows[identifier], score, match, parents.get(rows[identifier][0].parent_id))
                 for score, identifier, match in candidates[:top_k]]
        return RetrievalResult(items, trace)

    async def _keyword_rows(self, dataset_id: str, index_id: str, tokens: list[str]) -> list[DocumentSegmentRecord]:
        query = select(DocumentSegmentRecord).where(
            DocumentSegmentRecord.dataset_id == dataset_id,
            DocumentSegmentRecord.dataset_index_id == index_id,
            DocumentSegmentRecord.deleted_at.is_(None),
            DocumentSegmentRecord.status == "completed",
            DocumentSegmentRecord.keywords.is_not(None),
        )
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            query = query.where(DocumentSegmentRecord.keywords.op("&&")(tokens))
        result = await self.session.execute(query)
        return [row for row in result.scalars() if set(row.keywords or []).intersection(tokens)]

    async def _embed(self, query: str, model: str | None) -> tuple[float, ...]:
        if self.embedder is None:
            raise RuntimeError("embedding provider is not configured")
        if callable(self.embedder):
            value = self.embedder(query)
        else:
            value = self.embedder.embed(model, [query])
        value = await value if inspect.isawaitable(value) else value
        if hasattr(value, "vectors"):
            return tuple(value.vectors[0])
        if isinstance(value, Sequence) and value and isinstance(value[0], Sequence):
            return tuple(value[0])
        return tuple(value)

    @staticmethod
    def _item(row: tuple[DocumentSegmentRecord, DocumentRecord], score: float, match: str,
              parent: DocumentSegmentRecord | None = None) -> RetrievalItem:
        segment, document = row
        return RetrievalItem(segment.id, parent.content if parent is not None else segment.content, round(float(score), 6), match,
                             segment.document_id, document.name, segment.source_metadata, segment.parent_id)
