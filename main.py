from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4
from rag_modules.api.knowledge_base_api import router as knowledge_base_router
from rag_modules.api.file_api import router as file_router
from rag_modules.config.settings import settings
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "knowledge_bases.json"
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"
DEFAULT_EMBEDDING_DIMENSIONS = {
    "bge-large-zh": 1024,
    "bge-m3": 1024,
    "text-embedding-3-large": 3072,
}





def safe_collection_name(value: str) -> str:
    name = re.sub(r"[^0-9A-Za-z_]", "_", value.strip())
    if not name or not re.match(r"^[A-Za-z_]", name):
        name = f"kb_{name}"
    return name[:255]


# # Milvus 连接参数只从环境变量读取，避免把凭据写进前端或仓库数据。
# MILVUS_URI = settings.db_milvus_uri
# MILVUS_TOKEN =settings.db_milvus_token
# MILVUS_USER = settings.db_milvus_user
# MILVUS_PASSWORD = settings.db_milvus_password
# MILVUS_DATABASE = settings.db_milvus_database
# MILVUS_COLLECTION_PREFIX = settings.db_milvus_collection_prefix
# MILVUS_CONNECT_TIMEOUT = float(os.getenv("MILVUS_CONNECT_TIMEOUT", "5"))
# MILVUS_ENABLED = settings.db_milvus_enable

















#
# class MilvusHealth(BaseModel):
#     enabled: bool
#     status: Literal["connected", "disabled", "missing_dependency", "error"]
#     uri: str
#     database: str
#     collection_prefix: str
#     collection_count: int | None = None
#     message: str = ""
#
#
# store_lock = threading.Lock()
app = FastAPI(title="知识库管理")
# 注册子路由
app.include_router(knowledge_base_router, tags=["知识库"])
app.include_router(file_router, tags=["文件管理"])
#
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )
#
#
# def now_local() -> datetime:
#     return datetime.now(timezone.utc).astimezone()
#
#
# def default_collection_name(knowledge_base_id: str) -> str:
#     return safe_collection_name(f"{MILVUS_COLLECTION_PREFIX}_{knowledge_base_id}")
#
#
# def normalize_vector_store(
#     knowledge_base_id: str,
#     embedding_model: str,
#     vector_store: VectorStoreConfig,
# ) -> VectorStoreConfig:
#     collection_name = vector_store.collection_name.strip()
#     if not collection_name:
#         collection_name = default_collection_name(knowledge_base_id)
#     dimension = vector_store.embedding_dimension
#     if dimension <= 0:
#         dimension = DEFAULT_EMBEDDING_DIMENSIONS.get(embedding_model, 1024)
#     return vector_store.model_copy(
#         update={
#             "collection_name": safe_collection_name(collection_name),
#             "embedding_dimension": dimension,
#         }
#     )
#
#
# def get_milvus_client():
#     if not MILVUS_ENABLED:
#         raise RuntimeError("Milvus 接入已禁用，请设置 MILVUS_ENABLED=true")
#     try:
#         from pymilvus import MilvusClient
#     except ImportError as exc:
#         raise RuntimeError("pymilvus 未安装，请先执行 `python -m pip install -e .`") from exc
#
#     token = MILVUS_TOKEN or (
#         f"{MILVUS_USER}:{MILVUS_PASSWORD}" if MILVUS_USER and MILVUS_PASSWORD else None
#     )
#     kwargs = {"uri": MILVUS_URI}
#     if token:
#         kwargs["token"] = token
#     client = MilvusClient(**kwargs)
#     if MILVUS_DATABASE and MILVUS_DATABASE != "default" and hasattr(client, "use_database"):
#         client.use_database(db_name=MILVUS_DATABASE)
#     return client
#
#
# def ensure_milvus_collection(vector_store: VectorStoreConfig) -> None:
#     if not vector_store.auto_create_collection:
#         return
#     client = get_milvus_client()
#     collection_name = vector_store.collection_name
#     # 每个知识库对应一个 collection，便于后续独立导入、重建和删除。
#     existing = client.list_collections(timeout=MILVUS_CONNECT_TIMEOUT)
#     if collection_name in existing:
#         return
#     client.create_collection(
#         collection_name=collection_name,
#         dimension=vector_store.embedding_dimension,
#         primary_field_name="id",
#         id_type="string",
#         vector_field_name="embedding",
#         metric_type=vector_store.metric_type,
#         auto_id=False,
#         max_length=128,
#         timeout=MILVUS_CONNECT_TIMEOUT,
#     )
#
#
# def drop_milvus_collection(vector_store: VectorStoreConfig) -> None:
#     if not MILVUS_ENABLED or not vector_store.auto_create_collection:
#         return
#     client = get_milvus_client()
#     collections = client.list_collections(timeout=MILVUS_CONNECT_TIMEOUT)
#     if vector_store.collection_name in collections:
#         client.drop_collection(
#             collection_name=vector_store.collection_name,
#             timeout=MILVUS_CONNECT_TIMEOUT,
#         )
#
#
# def read_store() -> list[KnowledgeBase]:
#     if not DATA_FILE.exists():
#         return []
#     with DATA_FILE.open("r", encoding="utf-8") as file:
#         raw_items = json.load(file)
#     items: list[KnowledgeBase] = []
#     for raw_item in raw_items:
#         item = KnowledgeBase.model_validate(raw_item)
#         raw_vector_store = raw_item.get("vector_store") or {}
#         vector_store = normalize_vector_store(
#             item.id, item.embedding_model, item.vector_store
#         )
#         if "embedding_dimension" not in raw_vector_store:
#             vector_store = vector_store.model_copy(
#                 update={
#                     "embedding_dimension": DEFAULT_EMBEDDING_DIMENSIONS.get(
#                         item.embedding_model, 1024
#                     )
#                 }
#             )
#         items.append(
#             item.model_copy(
#                 update={
#                     "vector_store": vector_store,
#                 }
#             )
#         )
#     return items
#
#
# def write_store(items: list[KnowledgeBase]) -> None:
#     DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
#     payload = [item.model_dump(mode="json") for item in items]
#     with DATA_FILE.open("w", encoding="utf-8") as file:
#         json.dump(payload, file, ensure_ascii=False, indent=2)
#         file.write("\n")
#
#
# @app.get("/api/health")
# def health() -> dict[str, str]:
#     return {"status": "ok"}
#
#
# @app.get("/api/milvus/health", response_model=MilvusHealth)
# def milvus_health() -> MilvusHealth:
#     if not MILVUS_ENABLED:
#         return MilvusHealth(
#             enabled=False,
#             status="disabled",
#             uri=MILVUS_URI,
#             database=MILVUS_DATABASE,
#             collection_prefix=MILVUS_COLLECTION_PREFIX,
#             message="Milvus 接入已禁用",
#         )
#     try:
#         client = get_milvus_client()
#         collections = client.list_collections(timeout=MILVUS_CONNECT_TIMEOUT)
#         return MilvusHealth(
#             enabled=True,
#             status="connected",
#             uri=MILVUS_URI,
#             database=MILVUS_DATABASE,
#             collection_prefix=MILVUS_COLLECTION_PREFIX,
#             collection_count=len(collections),
#             message="Milvus 连接正常",
#         )
#     except RuntimeError as exc:
#         status = "missing_dependency" if "pymilvus" in str(exc) else "error"
#         return MilvusHealth(
#             enabled=True,
#             status=status,
#             uri=MILVUS_URI,
#             database=MILVUS_DATABASE,
#             collection_prefix=MILVUS_COLLECTION_PREFIX,
#             message=str(exc),
#         )
#     except Exception as exc:
#         return MilvusHealth(
#             enabled=True,
#             status="error",
#             uri=MILVUS_URI,
#             database=MILVUS_DATABASE,
#             collection_prefix=MILVUS_COLLECTION_PREFIX,
#             message=f"Milvus 连接失败：{exc}",
#         )
#
#
# @app.get("/api/knowledge-bases", response_model=KnowledgeBaseListResponse)
# def list_knowledge_bases(
#     q: str = "",
#     status: str = Query(default="all"),
#     visibility: str = Query(default="all"),
# ) -> KnowledgeBaseListResponse:
#     with store_lock:
#         items = read_store()
#
#     normalized_query = q.strip().lower()
#     if normalized_query:
#         items = [
#             item
#             for item in items
#             if normalized_query in item.name.lower()
#             or normalized_query in item.description.lower()
#             or normalized_query in item.category.lower()
#             or any(normalized_query in tag.lower() for tag in item.tags)
#         ]
#
#     if status != "all":
#         items = [item for item in items if item.status == status]
#
#     if visibility != "all":
#         items = [item for item in items if item.visibility == visibility]
#
#     items.sort(key=lambda item: item.updated_at, reverse=True)
#     return KnowledgeBaseListResponse(items=items, total=len(items))
#
#
# @app.get("/api/knowledge-bases/stats", response_model=KnowledgeBaseStats)
# def knowledge_base_stats() -> KnowledgeBaseStats:
#     with store_lock:
#         items = read_store()
#
#     return KnowledgeBaseStats(
#         total=len(items),
#         ready=sum(1 for item in items if item.status == "ready"),
#         indexing=sum(1 for item in items if item.status == "indexing"),
#         draft=sum(1 for item in items if item.status == "draft"),
#         documents=sum(item.document_count for item in items),
#         chunks=sum(item.chunk_count for item in items),
#     )
#
#
# @app.post("/api/knowledge-bases", response_model=KnowledgeBase, status_code=201)
# def create_knowledge_base(payload: KnowledgeBaseCreate) -> KnowledgeBase:
#     timestamp = now_local()
#     tags = [tag.strip() for tag in payload.tags if tag.strip()]
#     item_id = f"kb-{uuid4().hex[:12]}"
#     vector_store = normalize_vector_store(
#         item_id, payload.embedding_model, payload.vector_store
#     )
#     if (
#         "vector_store" not in payload.model_fields_set
#         or "embedding_dimension" not in payload.vector_store.model_fields_set
#     ):
#         vector_store = vector_store.model_copy(
#             update={
#                 "embedding_dimension": DEFAULT_EMBEDDING_DIMENSIONS.get(
#                     payload.embedding_model, 1024
#                 )
#             }
#         )
#
#     with store_lock:
#         items = read_store()
#         if any(existing.name == payload.name.strip() for existing in items):
#             raise HTTPException(status_code=409, detail="知识库名称已存在")
#
#     try:
#         ensure_milvus_collection(vector_store)
#     except Exception as exc:
#         raise HTTPException(status_code=503, detail=str(exc)) from exc
#
#     item = KnowledgeBase(
#         id=item_id,
#         name=payload.name.strip(),
#         description=payload.description.strip(),
#         category=payload.category.strip() or "通用知识",
#         owner=payload.owner.strip() or "当前用户",
#         visibility=payload.visibility,
#         embedding_model=payload.embedding_model.strip() or "bge-large-zh",
#         retrieval_config=payload.retrieval_config,
#         vector_store=vector_store,
#         status="draft",
#         tags=tags[:6],
#         created_at=timestamp,
#         updated_at=timestamp,
#     )
#
#     with store_lock:
#         items = read_store()
#         if any(existing.name == item.name for existing in items):
#             raise HTTPException(status_code=409, detail="知识库名称已存在")
#         items.append(item)
#         write_store(items)
#
#     return item
#
#
# @app.delete("/api/knowledge-bases/{knowledge_base_id}", status_code=204)
# def delete_knowledge_base(knowledge_base_id: str) -> None:
#     with store_lock:
#         items = read_store()
#         target = next((item for item in items if item.id == knowledge_base_id), None)
#         if target is None:
#             raise HTTPException(status_code=404, detail="知识库不存在")
#         next_items = [item for item in items if item.id != knowledge_base_id]
#
#     try:
#         # 先删除 Milvus collection，再更新本地元数据，避免留下已登记但不可检索的知识库。
#         drop_milvus_collection(target.vector_store)
#     except Exception as exc:
#         raise HTTPException(status_code=503, detail=str(exc)) from exc
#
#     with store_lock:
#         write_store(next_items)
#
#
# if FRONTEND_DIST.exists():
#     app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")
#
#
# @app.get("/{path:path}", response_model=None)
# def serve_frontend(path: str = "") -> FileResponse | dict[str, str]:
#     index_file = FRONTEND_DIST / "index.html"
#     if index_file.exists():
#         return FileResponse(index_file)
#     return {
#         "message": "Frontend is not built yet. Run `cd frontend && npm install && npm run build` first."
#     }
