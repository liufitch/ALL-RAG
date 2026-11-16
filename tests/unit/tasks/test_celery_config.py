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


def test_cleanup_is_scheduled_and_routed_to_consumed_maintenance_queue():
    dispatch_task = "rag_modules.tasks.document_cleanup_tasks.dispatch_pending_cleanup"
    cleanup_task = "rag_modules.tasks.document_cleanup_tasks.cleanup_document"
    scheduled = [entry for entry in celery_app.conf.beat_schedule.values() if entry["task"] == dispatch_task]
    assert len(scheduled) == 1
    assert scheduled[0]["schedule"] > 0
    assert scheduled[0]["options"]["queue"] == "maintenance"
    for task in [dispatch_task, cleanup_task]:
        assert celery_app.amqp.router.route({}, task)["queue"].name == "maintenance"
