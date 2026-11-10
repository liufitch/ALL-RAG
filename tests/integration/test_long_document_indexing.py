"""Opt-in, real-model long-document acceptance in an isolated PostgreSQL schema.

RUN_LONG_INDEXING=1 PERF_SOURCE_PDF=/path/to/source.pdf python -m pytest \
  tests/integration/test_long_document_indexing.py -v -s
Uses configured host-accessible MinIO, Milvus, Neo4j and model services. No
existing dataset is changed and no message is published to production queues.
"""

import asyncio
import json
import os
import re
from pathlib import Path
import time
from uuid import uuid4

from fastapi import UploadFile
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from rag_modules.api.dto.knowledge_base.knowledgeBaseCreate import KnowledgeBaseCreate
from rag_modules.api.dto.process_rule import ProcessRuleConfirmationRequest
from rag_modules.config.settings import settings
from rag_modules.db.base import Base
from rag_modules.db.models import DatasetIndexRecord, DocumentRecord, DocumentSegmentRecord, IndexingJobDocumentRecord, IndexingJobRecord
from rag_modules.graph.factory import get_graph_store
from rag_modules.indexing.progress import DatabaseProgressReporter
from rag_modules.object_storage.factory import get_object_storage
from rag_modules.repositories.document_repository import DocumentRepository
from rag_modules.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag_modules.repositories.process_rule_repository import ProcessRuleRepository
from rag_modules.retrieval import RetrievalService
from rag_modules.services.document_service import DocumentService
from rag_modules.services.knowledge_base_service import KnowledgeBaseService
from rag_modules.services.process_rule_service import ProcessRuleService
from rag_modules.tasks import indexing_tasks
from rag_modules.vector_stores.factory import get_vector_store
from rag_modules.embeddings import OpenAICompatibleEmbeddingClient


pytestmark = [pytest.mark.integration, pytest.mark.e2e]


@pytest.mark.asyncio
async def test_real_long_document_completes_and_resumes_graph_without_reembedding(monkeypatch):
    if os.getenv("RUN_LONG_INDEXING") != "1":
        pytest.skip("set RUN_LONG_INDEXING=1 and PERF_SOURCE_PDF for real-model acceptance")
    source = Path(os.environ["PERF_SOURCE_PDF"])
    expected = int(os.getenv("PERF_EXPECTED_CHILDREN", "3302"))
    artifact = Path(os.getenv("PERF_ARTIFACT", "data/indexing-performance-2026-11-10/full-pipeline.json"))
    artifact.parent.mkdir(parents=True, exist_ok=True)
    resume = os.getenv("PERF_RESUME") == "1"
    saved_report = json.loads(artifact.read_text()) if resume else None
    schema = saved_report["resume_schema"] if resume else "indexing_perf_" + uuid4().hex
    assert re.fullmatch(r"indexing_perf_[0-9a-f]{32}", schema)
    admin = create_async_engine(settings.sqlalchemy_database_uri, poolclass=NullPool, hide_parameters=True)
    engine = create_async_engine(settings.sqlalchemy_database_uri, poolclass=NullPool, hide_parameters=True,
                                 connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    started = time.perf_counter()
    report = {"source_bytes": source.stat().st_size, "expected_children": expected,
              "embedding_batch_size": settings.embedding.get_model(settings.embedding.default_model).batch_size,
              "embedding_concurrency": settings.embedding.max_concurrency,
              "graph_concurrency": settings.graph.max_concurrency,
              "progress": [], "passes": [], "embedding_segments_requested": 0, "graph_requests": 0}
    if resume:
        report = saved_report
        started -= float(report["elapsed_seconds"])
    report["resume_schema"] = schema
    forced_yield = any(item["stage"] == "graph-extract" and item["processed"] >= 32 for item in report["progress"])
    collection = object_key = index_id = None
    storage = get_object_storage()
    graph = await asyncio.to_thread(get_graph_store)
    vector_store = get_vector_store()

    def save():
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        artifact.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    class MeasuredProgress(DatabaseProgressReporter):
        async def update(self, stage, progress, processed_segments, total_segments=None):
            nonlocal forced_yield
            await super().update(stage, progress, processed_segments, total_segments)
            if not report["progress"] or stage != report["progress"][-1]["stage"] or processed_segments >= report["progress"][-1]["processed"] + 100:
                item = {"stage": stage, "processed": processed_segments, "total": total_segments,
                        "elapsed_seconds": round(time.perf_counter() - started, 3)}
                report["progress"].append(item)
                print(json.dumps(item), flush=True)
                save()
            if stage == "graph-extract" and processed_segments >= 32 and not forced_yield:
                forced_yield = True
                self.deadline = self.clock() - 1

    monkeypatch.setattr(indexing_tasks, "DatabaseProgressReporter", MeasuredProgress)
    try:
        if resume:
            async with factory() as session:
                row = await session.get(IndexingJobDocumentRecord, report["job_document_id"])
                assert row is not None and row.status in {"pending", "retry_wait"}
                job = await session.get(IndexingJobRecord, row.job_id)
                assert job is not None and job.target_index_id == report["index_id"]
                index = await session.get(DatasetIndexRecord, job.target_index_id)
                assert index is not None and index.dataset_id == report["dataset_id"]
                assert job.dataset_id == index.dataset_id
                document = await session.get(DocumentRecord, row.document_id)
                assert document is not None and document.dataset_id == index.dataset_id
                assert source.stat().st_size == report["source_bytes"]
                assert expected == report["expected_children"]
                # Enable external cleanup only after the complete ownership
                # chain has been checked inside this isolated schema.
                index_id = index.id
                job_document_id = row.id
                object_key = document.data_source_info["object_key"]
                collection = index.collection_name
        else:
            async with admin.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                datasets = KnowledgeBaseRepository(session)
                dataset = await KnowledgeBaseService(datasets).create_knowledge_base(
                    KnowledgeBaseCreate(name="Long document performance acceptance", permission="only_me"))
                documents = DocumentService(DocumentRepository(session), datasets, storage, settings.upload)
                with source.open("rb") as file:
                    document = await documents.upload_one(dataset.id, UploadFile(filename="acceptance.pdf", file=file), "performance-test")
                saved_document = await session.get(DocumentRecord, document.id)
                object_key = saved_document.data_source_info["object_key"]
                confirmation = await ProcessRuleService(ProcessRuleRepository(session)).confirm(
                    dataset.id, ProcessRuleConfirmationRequest(document_ids=[document.id], indexing_technique="high_quality",
                        embedding_model=settings.embedding.default_model,
                        segmentation={"mode": "parent_child", "parent_mode": "paragraph", "parent_max_chunk_length": 4096,
                                      "child_max_chunk_length": 200, "child_overlap": 20, "separator": "\n"}), "performance-test")
                job_document_id, index_id = confirmation.job_documents[0].id, confirmation.job.target_index_id
                report.update(dataset_id=dataset.id, index_id=index_id, job_document_id=job_document_id)
                save()

        for _ in range(100):
            async with factory() as session:
                runner = await indexing_tasks.build_task_runner(session, job_document_id=job_document_id)
                embed = runner.engine._embedding.embed
                extract = runner.engine._graph_indexer.extractor.extract

                async def measured_embed(model, texts):
                    report["embedding_segments_requested"] += len(texts)
                    return await embed(model, texts)

                async def measured_extract(content):
                    report["graph_requests"] += 1
                    return await extract(content)

                runner.engine._embedding.embed = measured_embed
                runner.engine._graph_indexer.extractor.extract = measured_extract
                before = report["embedding_segments_requested"]
                await runner.run(job_document_id)
                row = await session.get(IndexingJobDocumentRecord, job_document_id)
                report["passes"].append({"status": row.status, "attempt": row.attempt,
                                          "embedding_segments_requested": report["embedding_segments_requested"] - before,
                                          "error_code": row.error_code})
                index = await session.get(DatasetIndexRecord, index_id)
                collection = index.collection_name
                save()
                if row.status == "completed":
                    assert index.status == "active"
                    break
                assert row.status in {"pending", "retry_wait"}, report["passes"][-1]
                if row.status == "retry_wait":
                    await asyncio.sleep(30)
        else:
            pytest.fail("Indexing did not finish after 100 resumable passes")

        async with factory() as session:
            children = await session.scalar(select(func.count()).select_from(DocumentSegmentRecord).where(
                DocumentSegmentRecord.dataset_index_id == index_id, DocumentSegmentRecord.index_type == "child",
                DocumentSegmentRecord.status == "completed", DocumentSegmentRecord.embedding_status == "completed"))
            parents = await session.scalar(select(func.count()).select_from(DocumentSegmentRecord).where(
                DocumentSegmentRecord.dataset_index_id == index_id, DocumentSegmentRecord.index_type == "parent",
                DocumentSegmentRecord.status == "completed"))
            vectors = await asyncio.to_thread(vector_store.count, collection)
            rows, _, _ = await asyncio.to_thread(graph.driver.execute_query,
                "MATCH(s:SourceSegment {dataset_index_id:$index_id}) RETURN count(s) AS count, count(s.extraction_fingerprint) AS checkpoints",
                index_id=index_id, database_=graph.database)
            report.update(children=children, parents=parents, vectors=vectors,
                          graph_segments=rows[0]["count"], graph_checkpoints=rows[0]["checkpoints"])
            assert children == vectors == report["graph_segments"] == report["graph_checkpoints"] == expected
            assert forced_yield and len(report["passes"]) >= 2
            assert report["passes"][-1]["embedding_segments_requested"] == 0
            async with OpenAICompatibleEmbeddingClient(settings.embedding) as embedder:
                evidence = await RetrievalService(session, vector_store=vector_store, embedder=embedder).retrieve(
                    report["dataset_id"], "智能体如何使用工具", mode="vector", score_threshold=0)
                assert evidence.items
                report["vector_evidence_count"] = len(evidence.items)
            report["status"] = "passed"
            save()
            print(json.dumps({k: v for k, v in report.items() if k not in {"progress", "passes"}}), flush=True)
    finally:
        save()
        if os.getenv("PERF_KEEP_ON_FAILURE") == "1" and report.get("status") != "passed":
            await engine.dispose()
            await admin.dispose()
        else:
            if collection:
                await asyncio.to_thread(vector_store.drop_collection, collection)
            if index_id:
                await asyncio.to_thread(graph.driver.execute_query,
                    "MATCH(n) WHERE n.dataset_index_id=$index_id DETACH DELETE n", index_id=index_id, database_=graph.database)
            if object_key:
                await storage.remove_object(object_key)
            await engine.dispose()
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()
