from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from rag_modules.api.dto.retrieval import RetrievalRequest, RetrievalResponse, RetrievalItemResponse
from rag_modules.db.session import get_db_session
from rag_modules.embeddings.openai_compatible import OpenAICompatibleEmbeddingClient
from rag_modules.embeddings import EmbeddingError
from rag_modules.config.settings import settings
from rag_modules.graph.factory import get_graph_store
from rag_modules.retrieval import RetrievalService
from rag_modules.vector_stores.factory import get_vector_store
from rag_modules.db.models import DatasetRecord

router = APIRouter(prefix="/api/knowledge_base/{dataset_id}", tags=["检索"])


def _retrieval_options(payload: RetrievalRequest, saved: dict) -> dict:
    params = payload.model_dump(exclude_none=True)
    params.pop("query", None)
    for key, value in saved.items():
        params.setdefault(key, value)
    return {
        "mode": params.get("mode", "hybrid"),
        "top_k": params.get("top_k", 5),
        "score_threshold": params.get("score_threshold", 0.3),
        "semantic_weight": params.get("semantic_weight", 0.7),
        "keyword_weight": params.get("keyword_weight", 0.3),
        "rerank_enabled": params.get("rerank_enabled", False),
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def get_retrieval_service(db=Depends(get_db_session)):
    graph_store = await asyncio.to_thread(get_graph_store)
    embedder = OpenAICompatibleEmbeddingClient(settings.embedding)
    service = RetrievalService(
        db,
        vector_store=get_vector_store(),
        embedder=embedder,
        graph_store=graph_store,
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
    db=Depends(get_db_session),
) -> RetrievalResponse:
    dataset = await db.scalar(select(DatasetRecord).where(
        DatasetRecord.id == dataset_id, DatasetRecord.deleted_at.is_(None),
    ))
    saved = dataset.retrieval_model_config if dataset and dataset.retrieval_model_config else {}
    try:
        result = await service.retrieve(
            dataset_id,
            payload.query,
            **_retrieval_options(payload, saved),
        )
    except EmbeddingError as exc:
        status = 422 if exc.code == "EMBEDDING_INPUT_INVALID" else 503
        raise HTTPException(status_code=status, detail=exc.safe_message) from exc
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


@router.post("/retrieve/stream")
async def retrieve_stream(
    dataset_id: str,
    payload: RetrievalRequest,
    service: RetrievalService = Depends(get_retrieval_service),
    db=Depends(get_db_session),
) -> StreamingResponse:
    dataset = await db.scalar(select(DatasetRecord).where(
        DatasetRecord.id == dataset_id, DatasetRecord.deleted_at.is_(None),
    ))
    if dataset is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    options = _retrieval_options(payload, dataset.retrieval_model_config or {})

    async def events():
        try:
            result = await service.retrieve(dataset_id, payload.query, **options)
            for reference, item in enumerate(result.items, start=1):
                yield _sse("hit", {
                    "reference": reference,
                    "segment_id": item.segment_id,
                    "context_segment_id": item.parent_id or item.segment_id,
                    "document_id": item.document_id,
                    "document_name": item.document_name,
                    "content": item.content,
                    "source_metadata": item.source_metadata or {},
                    "score": item.score,
                    "match_type": item.match_type,
                })
            yield _sse("done", {"count": len(result.items)})
        except EmbeddingError as exc:
            if exc.code == "EMBEDDING_INPUT_INVALID":
                yield _sse("error", {"code": "RETRIEVAL_INPUT_INVALID", "message": "检索问题无效。"})
            else:
                yield _sse("error", {"code": "RETRIEVAL_FAILED", "message": "检索服务暂时不可用。"})
        except ValueError:
            yield _sse("error", {"code": "RETRIEVAL_INPUT_INVALID", "message": "检索问题无效。"})
        except Exception:
            yield _sse("error", {"code": "RETRIEVAL_FAILED", "message": "检索服务暂时不可用。"})

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })
