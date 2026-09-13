"""索引 Worker 的 Celery 入口。

当前入口先固定消息契约和异步边界；实际执行器应在这里加载持久化任务快照，
而不是把解析、Embedding 或 Milvus 调用放回 FastAPI 请求线程。
"""

from __future__ import annotations

from .celery_app import celery_app


@celery_app.task(name="rag_modules.tasks.indexing_tasks.index_document")
def index_document(*, dataset_id: str, document_id: str) -> None:
    """处理一个文档索引消息。

    消息参数保持为标识符，便于至少一次投递和重复消息下的幂等领取。
    具体执行由持久化 job-document 编排器接管；未找到任务时应按幂等成功处理。
    """
    # 先保留稳定入口，避免上传成功后消息落入未注册任务；完整 Worker
    # 接入时应在此处调用任务领取、DocumentIndexingEngine 和状态汇总流程。
    return None