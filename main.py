from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from rag_modules.api.file_api import router as file_router
from rag_modules.api.indexing_options_api import router as indexing_options_router
from rag_modules.api.indexing_preview_api import router as indexing_preview_router
from rag_modules.api.indexing_configuration_api import router as indexing_configuration_router
from rag_modules.api.knowledge_base_api import router as knowledge_base_router
from rag_modules.api.knowledge_base_settings_api import router as knowledge_base_settings_router
from rag_modules.api.retrieval_api import router as retrieval_router
from rag_modules.api.indexing_jobs_api import router as indexing_jobs_router
from rag_modules.api.ingest_api import router as ingest_router


def create_app() -> FastAPI:
    app = FastAPI(title="知识库管理")
    app.include_router(knowledge_base_router, tags=["知识库"])
    app.include_router(knowledge_base_settings_router)
    app.include_router(file_router, tags=["文件管理"])
    app.include_router(indexing_options_router)
    app.include_router(indexing_preview_router)
    app.include_router(indexing_configuration_router)
    app.include_router(retrieval_router)
    app.include_router(indexing_jobs_router)
    app.include_router(ingest_router)

    frontend_dist = Path(__file__).resolve().parent / "frontend" / "dist"
    if (frontend_dist / "index.html").is_file():
        # Register last so API routes retain priority over the frontend mount.
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    return app


app = create_app()

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
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        log_level="debug",
        reload=False  # 调试模式必须False
    )
