from celery import Celery
from kombu import Queue

from rag_modules.config.settings import settings


celery_app = Celery("graph_rag", broker=settings.broker.url)
celery_app.conf.update(
    result_backend=None,
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_delivery_mode=2,
    task_serializer="json",
    accept_content=["json"],
    task_queues=(
        Queue("indexing", durable=True),
        Queue("maintenance", durable=True),
    ),
    # 任务超时保护，防止索引任务卡死永不释放worker
    task_time_limit = 3600,
    task_soft_time_limit = 3500,
    # 任务名称不使用默认的主机名，方便日志追踪
    worker_task_log_format = "[%(asctime)s][%(task_name)s][%(task_id)s] %(message)s",
)

# 任务名称稳定后，API/Beat 可以只投递业务 ID；Worker 再从 PostgreSQL
# 加载不可变配置和 MinIO 对象引用，避免消息携带文件内容或敏感配置。
celery_app.conf.task_routes = {
    "rag_modules.tasks.indexing_tasks.index_document": {"queue": "indexing"},
}
