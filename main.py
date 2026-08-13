from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "knowledge_bases.json"
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"


class RetrievalConfig(BaseModel):
    mode: Literal["vector", "full_text", "hybrid"] = "vector"
    top_k: int = Field(default=5, ge=1, le=100)
    score_threshold: float = Field(default=0.3, ge=0, le=1)
    rerank_enabled: bool = False
    rerank_model: str = Field(default="bge-reranker-large", max_length=80)
    semantic_weight: float = Field(default=0.7, ge=0, le=1)
    keyword_weight: float = Field(default=0.3, ge=0, le=1)


class KnowledgeBase(BaseModel):
    id: str
    name: str
    description: str = ""
    category: str = "通用知识"
    owner: str = "当前用户"
    visibility: Literal["private", "team", "public"] = "private"
    embedding_model: str = "bge-large-zh"
    retrieval_config: RetrievalConfig = Field(default_factory=RetrievalConfig)
    status: Literal["draft", "indexing", "ready", "failed"] = "draft"
    document_count: int = 0
    chunk_count: int = 0
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    category: str = Field(default="通用知识", max_length=40)
    owner: str = Field(default="当前用户", max_length=40)
    visibility: Literal["private", "team", "public"] = "private"
    embedding_model: str = Field(default="bge-large-zh", max_length=80)
    retrieval_config: RetrievalConfig = Field(default_factory=RetrievalConfig)
    tags: list[str] = Field(default_factory=list)


class KnowledgeBaseListResponse(BaseModel):
    items: list[KnowledgeBase]
    total: int


class KnowledgeBaseStats(BaseModel):
    total: int
    ready: int
    indexing: int
    draft: int
    documents: int
    chunks: int


store_lock = threading.Lock()
app = FastAPI(title="Graph-RAG Knowledge Base Console")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def read_store() -> list[KnowledgeBase]:
    if not DATA_FILE.exists():
        return []
    with DATA_FILE.open("r", encoding="utf-8") as file:
        return [KnowledgeBase.model_validate(item) for item in json.load(file)]


def write_store(items: list[KnowledgeBase]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = [item.model_dump(mode="json") for item in items]
    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/knowledge-bases", response_model=KnowledgeBaseListResponse)
def list_knowledge_bases(
    q: str = "",
    status: str = Query(default="all"),
    visibility: str = Query(default="all"),
) -> KnowledgeBaseListResponse:
    with store_lock:
        items = read_store()

    normalized_query = q.strip().lower()
    if normalized_query:
        items = [
            item
            for item in items
            if normalized_query in item.name.lower()
            or normalized_query in item.description.lower()
            or normalized_query in item.category.lower()
            or any(normalized_query in tag.lower() for tag in item.tags)
        ]

    if status != "all":
        items = [item for item in items if item.status == status]

    if visibility != "all":
        items = [item for item in items if item.visibility == visibility]

    items.sort(key=lambda item: item.updated_at, reverse=True)
    return KnowledgeBaseListResponse(items=items, total=len(items))


@app.get("/api/knowledge-bases/stats", response_model=KnowledgeBaseStats)
def knowledge_base_stats() -> KnowledgeBaseStats:
    with store_lock:
        items = read_store()

    return KnowledgeBaseStats(
        total=len(items),
        ready=sum(1 for item in items if item.status == "ready"),
        indexing=sum(1 for item in items if item.status == "indexing"),
        draft=sum(1 for item in items if item.status == "draft"),
        documents=sum(item.document_count for item in items),
        chunks=sum(item.chunk_count for item in items),
    )


@app.post("/api/knowledge-bases", response_model=KnowledgeBase, status_code=201)
def create_knowledge_base(payload: KnowledgeBaseCreate) -> KnowledgeBase:
    timestamp = now_local()
    tags = [tag.strip() for tag in payload.tags if tag.strip()]
    item = KnowledgeBase(
        id=f"kb-{uuid4().hex[:12]}",
        name=payload.name.strip(),
        description=payload.description.strip(),
        category=payload.category.strip() or "通用知识",
        owner=payload.owner.strip() or "当前用户",
        visibility=payload.visibility,
        embedding_model=payload.embedding_model.strip() or "bge-large-zh",
        retrieval_config=payload.retrieval_config,
        status="draft",
        tags=tags[:6],
        created_at=timestamp,
        updated_at=timestamp,
    )

    with store_lock:
        items = read_store()
        if any(existing.name == item.name for existing in items):
            raise HTTPException(status_code=409, detail="知识库名称已存在")
        items.append(item)
        write_store(items)

    return item


@app.delete("/api/knowledge-bases/{knowledge_base_id}", status_code=204)
def delete_knowledge_base(knowledge_base_id: str) -> None:
    with store_lock:
        items = read_store()
        next_items = [item for item in items if item.id != knowledge_base_id]
        if len(next_items) == len(items):
            raise HTTPException(status_code=404, detail="知识库不存在")
        write_store(next_items)


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/{path:path}", response_model=None)
def serve_frontend(path: str = "") -> FileResponse | dict[str, str]:
    index_file = FRONTEND_DIST / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {
        "message": "Frontend is not built yet. Run `cd frontend && npm install && npm run build` first."
    }
