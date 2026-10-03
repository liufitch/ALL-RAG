from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from rag_modules.api.dto.retrieval import RetrievalRequest, RetrievalResponse, RetrievalItemResponse
from rag_modules.db.session import get_db_session
from rag_modules.embeddings.openai_compatible import OpenAICompatibleEmbeddingClient
from rag_modules.config.settings import settings
from rag_modules.retrieval import RetrievalService
from rag_modules.vector_stores.factory import get_vector_store

router = APIRouter(prefix="/api/knowledge_base/{dataset_id}", tags=["检索"])


async def get_retrieval_service(db=Depends(get_db_session)):
    embedder = OpenAICompatibleEmbeddingClient(settings.embedding)
    service = RetrievalService(
        db,
        vector_store=get_vector_store(),
        embedder=embedder,
    )
    try:
        yield service
    finally:
        await embedder.aclose()


@router.post("/retrieve", response_model=RetrievalResponse)
async def retrieve(
    dataset_id: str,
    payload: RetrievalRequest,
    service: RetrievalService = Depends(get_retrieval_service),
) -> RetrievalResponse:
    try:
        result = await service.retrieve(
            dataset_id,
            payload.query,
            mode=payload.mode,
            top_k=payload.top_k,
            score_threshold=payload.score_threshold,
            semantic_weight=payload.semantic_weight,
            keyword_weight=payload.keyword_weight,
            rerank_enabled=payload.rerank_enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RetrievalResponse(
        items=[RetrievalItemResponse(
            segment_id=item.segment_id, content=item.content, score=item.score,
            match_type=item.match_type, document_id=item.document_id,
            document_name=item.document_name, source_metadata=item.source_metadata,
            parent_id=item.parent_id,
        ) for item in result.items],
        rerank_trace=result.rerank_trace,
    )
