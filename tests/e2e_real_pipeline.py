import asyncio
import io
import os
import sys
import time
from uuid import uuid4

# 载入配置
from rag_modules.config.settings import settings
from rag_modules.db.session import SessionLocal
from rag_modules.db.models import (
    DatasetRecord,
    DocumentRecord,
    ProcessRuleRecord,
    IndexingJobRecord,
    IndexingJobDocumentRecord,
    DatasetIndexRecord,
)
from rag_modules.object_storage.factory import get_object_storage
from rag_modules.tasks.publisher import get_task_publisher
from rag_modules.retrieval import RetrievalService
from rag_modules.indexing.keywords import KeywordExtractor
from rag_modules.common import utcnow

async def run_e2e():
    print("--- 1. 验证基础服务连接 ---")
    storage = get_object_storage()
    await storage.ensure_bucket()
    print("MinIO bucket 确认正常:", settings.object_storage.bucket)

    dataset_id = uuid4().hex
    doc_id = uuid4().hex
    job_id = uuid4().hex
    job_doc_id = uuid4().hex
    index_id = uuid4().hex
    rule_id = uuid4().hex

    test_content = b"Graph-RAG is an innovative knowledge base system. It integrates vector search, full-text search, and knowledge graph paths for high precision question answering."
    object_key = f"datasets/{dataset_id}/documents/{doc_id}/source.txt"

    print("--- 2. 上传文件到 MinIO ---")
    await storage.put_stream(object_key, io.BytesIO(test_content), len(test_content), "text/plain")
    assert await storage.get_bytes(object_key, len(test_content) + 1) == test_content, "MinIO 写入验证失败"
    print("文件已写入 MinIO:", object_key)

    print("--- 3. 写入 PostgreSQL 业务事实 ---")
    rule_snapshot = {
        "indexing_technique": "economy",
        "embedding_model": None,
        "segmentation": {
            "mode": "general",
            "max_chunk_length": 500,
            "overlap": 50,
            "separator": "\n",
        },
    }

    async with SessionLocal() as session:
        ds = DatasetRecord(
            id=dataset_id,
            name="E2E Real DS",
            provider="local",
            permission="only_me",
            indexing_technique="economy",
            created_by="e2e-user",
        )
        rule = ProcessRuleRecord(
            id=rule_id,
            dataset_id=dataset_id,
            version=1,
            mode="general",
            rules=rule_snapshot["segmentation"],
            config_hash="e2e-hash",
            created_by="e2e-user",
        )
        doc = DocumentRecord(
            id=doc_id,
            dataset_id=dataset_id,
            position=0,
            data_source_type="upload_file",
            data_source_info={
                "storage": "minio",
                "bucket": settings.object_storage.bucket,
                "object_key": object_key,
                "original_filename": "sample.txt",
                "size": len(test_content),
                "content_type": "text/plain",
            },
            dataset_process_rule_id=rule_id,
            name="sample.txt",
            created_from="api",
            created_by="e2e-user",
            indexing_status="queued",
        )
        job = IndexingJobRecord(
            id=job_id,
            dataset_id=dataset_id,
            target_index_id=None,
            job_type="initial_index",
            scope="selected_documents",
            status="queued",
            indexing_technique="economy",
            segmentation_mode="general",
            process_rule=rule_snapshot,
            retrieval_config={},
            total_documents=1,
            created_by="e2e-user",
        )
        index = DatasetIndexRecord(
            id=index_id,
            dataset_id=dataset_id,
            created_by_job_id=job_id,
            index_type="economy",
            status="building",
            process_rule=rule_snapshot,
            retrieval_config={},
            config_hash="e2e-hash",
        )
        job_doc = IndexingJobDocumentRecord(
            id=job_doc_id,
            job_id=job_id,
            document_id=doc_id,
            status="queued",
        )
        session.add_all([ds, rule])
        await session.flush()
        session.add_all([doc, job])
        await session.flush()
        session.add(index)
        await session.flush()
        job.target_index_id = index_id
        session.add(job_doc)
        await session.commit()
    print("PostgreSQL 事务提交完成")

    print("--- 4. 向 RabbitMQ 投递任务 ---")
    publisher = get_task_publisher()
    task_id = publisher.dispatch_job_document(job_document_id=job_doc_id)
    print("任务已投递到 RabbitMQ, Celery task id:", task_id)

    print("--- 5. 轮询等待 Worker 消费与持久化更新 ---")
    for attempt in range(30):
        await asyncio.sleep(1)
        async with SessionLocal() as session:
            jd = await session.get(IndexingJobDocumentRecord, job_doc_id)
            idx = await session.get(DatasetIndexRecord, index_id)
            j = await session.get(IndexingJobRecord, job_id)
            print(f"[{attempt+1}s] job_status={j.status}, doc_status={jd.status}, progress={jd.progress}%, index_status={idx.status}")
            if jd.status == "completed" and idx.status == "active":
                print("Worker 执行成功，索引已激活！")
                break
            if jd.status == "failed" or j.status == "failed":
                raise RuntimeError(f"索引失败: error={jd.error}, code={jd.error_code}")
    else:
        raise TimeoutError("等待 Worker 索引超时")

    print("--- 6. 执行真实检索验证 ---")
    async with SessionLocal() as session:
        retriever = RetrievalService(session)
        result = await retriever.retrieve(
            dataset_id,
            query="precision question answering",
            mode="full_text",
            top_k=5,
            score_threshold=0.01,
        )
        print("检索结果条数:", len(result.items))
        assert len(result.items) > 0, "未召回期望分段"
        hit = result.items[0]
        print(f"命中分段 [score={hit.score}, match={hit.match_type}]: {hit.content[:80]}...")
        assert "innovative knowledge base system" in hit.content

    print("=== 全链路真实 E2E 验证完全通过! ===")

if __name__ == "__main__":
    asyncio.run(run_e2e())
