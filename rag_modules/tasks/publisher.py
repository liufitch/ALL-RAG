"""索引任务消息发布边界。

API 只在数据库写入完成后调用本模块，并且消息只携带业务 ID。
文件内容、对象存储凭据和完整配置必须留在 PostgreSQL/MinIO，不能进入 RabbitMQ。
"""

from __future__ import annotations

from celery import Celery

from .celery_app import celery_app


class TaskPublisher:
    """将已提交的文档索引任务投递到持久化索引队列。"""

    def __init__(self, app: Celery = celery_app) -> None:
        self._app = app

    def dispatch_document(self, *, dataset_id: str, document_id: str) -> str | None:
        """投递单文档任务并返回 broker 分配的 task id。

        这里不等待索引完成；Celery 的晚确认和数据库中的文档状态共同构成
        至少一次投递语义。调用方应把发布失败视为“待补投”，而不是删除已上传文档。
        """
        result = self._app.send_task(
            "rag_modules.tasks.indexing_tasks.index_document",
            kwargs={"dataset_id": dataset_id, "document_id": document_id},
            queue="indexing",
        )
        return getattr(result, "id", None)

    def dispatch_job_document(self, *, job_document_id: str) -> str | None:
        result = self._app.send_task(
            "rag_modules.tasks.indexing_tasks.index_document",
            kwargs={"job_document_id": job_document_id},
            queue="indexing",
        )
        return getattr(result, "id", None)


def get_task_publisher() -> TaskPublisher:
    """提供轻量发布器；不在导入阶段连接 RabbitMQ。"""
    return TaskPublisher()
