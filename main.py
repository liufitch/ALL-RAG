from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from rag_modules.api.file_api import router as file_router
from rag_modules.api.indexing_options_api import router as indexing_options_router
from rag_modules.api.indexing_preview_api import router as indexing_preview_router
from rag_modules.api.knowledge_base_api import router as knowledge_base_router


app = FastAPI(title="知识库管理")
# 注册子路由
app.include_router(knowledge_base_router, tags=["知识库"])
app.include_router(file_router, tags=["文件管理"])
app.include_router(indexing_options_router)
app.include_router(indexing_preview_router)

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )


# @app.get("/api/health")
# def health() -> dict[str, str]:
#     return {"status": "ok"}
