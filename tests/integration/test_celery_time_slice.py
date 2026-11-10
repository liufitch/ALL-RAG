"""Opt-in RabbitMQ/worker acceptance with a disposable database and queue.

RUN_CELERY_RESUME=1 python -m pytest tests/integration/test_celery_time_slice.py -s
Requires local PostgreSQL create-database permission, MinIO and RabbitMQ.
The test deletes its source after the first slice to prove durable preparation
is reused by the next actual Celery delivery.
"""

import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from fastapi import UploadFile
from starlette.datastructures import Headers
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from rag_modules.api.dto.knowledge_base.knowledgeBaseCreate import KnowledgeBaseCreate
from rag_modules.api.dto.process_rule import ProcessRuleConfirmationRequest
from rag_modules.config.settings import settings
from rag_modules.db.base import Base
from rag_modules.db.models import DatasetIndexRecord, DocumentRecord, DocumentSegmentRecord, IndexingJobDocumentRecord
from rag_modules.object_storage.factory import get_object_storage
from rag_modules.repositories.document_repository import DocumentRepository
from rag_modules.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag_modules.repositories.process_rule_repository import ProcessRuleRepository
from rag_modules.services.document_service import DocumentService
from rag_modules.services.knowledge_base_service import KnowledgeBaseService
from rag_modules.services.process_rule_service import ProcessRuleService
from rag_modules.tasks.celery_app import celery_app


pytestmark = [pytest.mark.integration, pytest.mark.e2e]


@pytest.mark.asyncio
async def test_real_celery_deliveries_resume_without_source_or_failure_attempts():
    if os.getenv("RUN_CELERY_RESUME") != "1":
        pytest.skip("set RUN_CELERY_RESUME=1 for isolated RabbitMQ/worker acceptance")
    name = "indexing_resume_" + uuid4().hex
    root = Path("data/indexing-performance-2026-11-10")
    root.mkdir(parents=True, exist_ok=True)
    admin = create_async_engine(settings.sqlalchemy_database_uri, poolclass=NullPool, hide_parameters=True)
    database = settings.database.model_copy(update={"database": name})
    engine = create_async_engine(database.uri, poolclass=NullPool, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    storage = get_object_storage()
    worker = object_key = None
    report = {"deliveries": 0, "states": []}
    log = (root / "celery-resume-worker.log").open("wb")
    started = time.monotonic()
    source_removed = False
    try:
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            datasets = KnowledgeBaseRepository(session)
            dataset = await KnowledgeBaseService(datasets).create_knowledge_base(
                KnowledgeBaseCreate(name="Celery slice acceptance", permission="only_me"))
            line = "知识库系统使用工具处理文档，向量索引保存检索证据，项目团队位于上海。" * 6
            document = await DocumentService(DocumentRepository(session), datasets, storage, settings.upload).upload_one(
                dataset.id, UploadFile(filename="resume.txt", file=BytesIO(((line + "\n") * 4500).encode()),
                                       headers=Headers({"content-type": "text/plain"})), "performance-test")
            object_key = (await session.get(DocumentRecord, document.id)).data_source_info["object_key"]
            created = await ProcessRuleService(ProcessRuleRepository(session)).confirm(dataset.id,
                ProcessRuleConfirmationRequest(document_ids=[document.id], indexing_technique="economy",
                    segmentation={"mode": "general", "max_chunk_length": 240, "overlap": 0, "separator": "\n"}), "performance-test")
            job_document_id, index_id = created.job_documents[0].id, created.job.target_index_id

        worker_env = {**os.environ, "DATABASE__DATABASE": name, "GRAPH__ENABLED": "false",
                      "INDEXING__TASK_SLICE_SECONDS": "1", "INDEXING__HEARTBEAT_INTERVAL_SECONDS": "0.1"}
        worker = subprocess.Popen([sys.executable, "-m", "celery", "-A", "rag_modules.tasks.celery_app:celery_app",
                                   "worker", "--pool=solo", "--concurrency=1", "--queues", name, "--hostname", name,
                                   "--without-mingle", "--without-gossip", "--without-heartbeat", "--loglevel=INFO"],
                                  env=worker_env, stdout=log, stderr=subprocess.STDOUT)
        while time.monotonic() - started < 300:
            assert worker.poll() is None, "isolated worker exited; inspect celery-resume-worker.log"
            async with factory() as session:
                row = await session.get(IndexingJobDocumentRecord, job_document_id)
                state = (row.status, row.progress, row.processed_segments)
                if not report["states"] or list(state) != report["states"][-1]:
                    report["states"].append(list(state))
                if row.status == "completed":
                    # Document completion precedes atomic dataset publication.
                    if (await session.get(DatasetIndexRecord, index_id)).status != "active":
                        await asyncio.sleep(.1)
                        continue
                    assert row.attempt == 1
                    assert source_removed and report["deliveries"] >= 2
                    count = await session.scalar(select(func.count()).select_from(DocumentSegmentRecord).where(
                        DocumentSegmentRecord.dataset_index_id == index_id, DocumentSegmentRecord.status == "completed"))
                    assert count == row.total_segments
                    report.update(status="passed", completed_segments=count, final_attempt=row.attempt,
                                  seconds=round(time.monotonic() - started, 3))
                    break
                assert row.status not in {"failed", "cancelled", "retry_wait"}, row.error
                if row.status == "pending":
                    if report["deliveries"] and not source_removed:
                        await storage.remove_object(object_key)
                        source_removed = True
                    row.status = "queued"
                    await session.commit()
                    await asyncio.to_thread(celery_app.send_task, "rag_modules.tasks.indexing_tasks.index_document",
                                            kwargs={"job_document_id": job_document_id}, queue=name)
                    report["deliveries"] += 1
            await asyncio.sleep(.1)
        else:
            pytest.fail("Celery continuation did not finish within 300 seconds")
    finally:
        if worker is not None:
            worker.terminate()
            try:
                await asyncio.to_thread(worker.wait, 10)
            except subprocess.TimeoutExpired:
                worker.kill()
                await asyncio.to_thread(worker.wait)
        log.close()
        if object_key and not source_removed:
            await storage.remove_object(object_key)
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
        if report["deliveries"]:
            with celery_app.connection_for_write() as connection:
                channel = connection.channel()
                channel.queue_delete(queue=name)
                if celery_app.amqp.queues[name].exchange.name == name:
                    channel.exchange_delete(exchange=name, if_unused=True)
                channel.close()
        (root / "celery-resume.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    assert report.get("status") == "passed"
