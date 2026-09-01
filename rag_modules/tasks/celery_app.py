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
)
