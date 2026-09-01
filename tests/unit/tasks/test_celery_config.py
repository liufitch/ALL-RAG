from rag_modules.tasks.celery_app import celery_app


def test_celery_uses_rabbitmq_with_late_ack_and_no_result_backend():
    assert celery_app.conf.broker_url.startswith("amqp://")
    assert celery_app.conf.result_backend is None
    assert celery_app.conf.task_ignore_result is True
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert {queue.name for queue in celery_app.conf.task_queues} == {
        "indexing",
        "maintenance",
    }
