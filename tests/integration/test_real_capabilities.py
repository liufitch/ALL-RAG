"""Opt-in checks with real model inference, PostgreSQL, Milvus, OCR and Neo4j.

RUN_REAL_ENV=1 python -m pytest tests/integration/test_real_capabilities.py -v -s
Enable the graph test with RUN_REAL_GRAPH=1 after configuring the graph model.
"""

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from rag_modules.config.settings import settings
from rag_modules.db.models import DatasetIndexRecord, DocumentSegmentRecord
from rag_modules.vector_stores.milvus import MilvusVectorStore


pytestmark = [pytest.mark.integration, pytest.mark.e2e]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "documents"


async def _database_snapshot(dataset_id):
    engine = create_async_engine(settings.sqlalchemy_database_uri, poolclass=NullPool, hide_parameters=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            indexes = list((await session.scalars(select(DatasetIndexRecord).where(
                DatasetIndexRecord.dataset_id == dataset_id,
            ))).all())
            active = next((index for index in indexes if index.status == "active"), None)
            rows = []
            if active:
                rows = list((await session.scalars(select(DocumentSegmentRecord).where(
                    DocumentSegmentRecord.dataset_index_id == active.id,
                    DocumentSegmentRecord.deleted_at.is_(None),
                    DocumentSegmentRecord.status == "completed",
                ))).all())
            return {
                "index_id": active.id if active else None,
                "dimension": active.embedding_dimension if active else None,
                "collection": active.collection_name if active else None,
                "collections": [index.collection_name for index in indexes if index.collection_name],
                "segments": [{"id": row.id, "document_id": row.document_id, "index_type": row.index_type,
                              "embedding_status": row.embedding_status, "keywords": row.keywords,
                              "parent_id": row.parent_id} for row in rows],
            }
    finally:
        await engine.dispose()


class RealEnvironment:
    def __init__(self, client, name):
        self.client = client
        self.datasets = []
        self.artifact = Path(os.getenv("REAL_ENV_ARTIFACTS", "data/real-environment-2026-10-27")) / f"{name}.json"
        self.report = {"steps": []}
        self.vector_store = MilvusVectorStore(config=settings.vector_store.model_copy(update={
            "enabled": True, "uri": os.getenv("REAL_MILVUS_URI", "http://127.0.0.1:19530"),
            "host": "127.0.0.1", "database": "default",
        }))
        self.model = self.json("GET", "/api/indexing/options")["defaults"]["embedding_model"]

    def save(self):
        self.artifact.parent.mkdir(parents=True, exist_ok=True)
        self.artifact.write_text(json.dumps(self.report, ensure_ascii=False, indent=2))

    def json(self, method, path, status=200, **kwargs):
        response = self.client.request(method, path, **kwargs)
        assert response.status_code == status, (method, path, response.status_code, response.text)
        result = response.json()
        self.report["steps"].append({"method": method, "path": path, "status": status, "result": result})
        self.save()
        return result

    def dataset(self, label):
        dataset = self.json("POST", "/api/knowledge_base", 201, json={
            "name": f"真实能力验收 {label} {uuid4().hex[:8]}", "permission": "only_me",
        })
        base = "/api/knowledge_base/" + dataset["id"]
        self.datasets.append(base)
        return base

    def upload(self, base, name, content, mime="text/plain"):
        result = self.json("POST", base + "/documents/upload", 201, files={"files": (name, content, mime)})
        assert not result["rejected"], result
        return result["documents"][0]["id"]

    def configure(self, base, documents, segmentation=None, technique="high_quality"):
        config = {"document_ids": documents, "indexing_technique": technique,
                  "embedding_model": self.model if technique == "high_quality" else None,
                  "segmentation": segmentation or {"mode": "general", "max_chunk_length": 180, "overlap": 20}}
        preview = self.json("POST", base + "/indexing/preview", json=config)
        assert preview["chunks"]
        job = self.json("POST", base + "/indexing/configure", 202, json=config)
        return self.wait(base, job["job_id"])

    def wait(self, base, job_id, expected="completed"):
        deadline = time.monotonic() + int(os.getenv("REAL_ENV_TIMEOUT", "600"))
        last = None
        while time.monotonic() < deadline:
            response = self.client.get(base + "/indexing/jobs/" + job_id)
            assert response.status_code == 200, response.text
            job = response.json()
            state = (job["status"], job["progress"])
            if state != last:
                print("JOB", job_id, state, flush=True)
                last = state
            if job["status"] in {"completed", "failed", "cancelled", "partial_success", "partially_completed"}:
                self.report["steps"].append({"job": job})
                self.save()
                assert job["status"] == expected, job
                return job
            time.sleep(0.5)
        pytest.fail(f"Job {job_id} did not finish: {last}")

    def retrieve(self, base, query, mode, top_k=5):
        return self.json("POST", base + "/retrieve", json={
            "query": query, "mode": mode, "top_k": top_k, "score_threshold": 0,
        })["items"]

    def snapshot(self, base):
        snapshot = asyncio.run(_database_snapshot(base.rsplit("/", 1)[1]))
        if snapshot["collection"]:
            snapshot["vector_count"] = self.vector_store.count(snapshot["collection"])
        self.report.setdefault("snapshots", []).append(snapshot)
        self.save()
        return snapshot

    def cleanup(self):
        if os.getenv("KEEP_REAL_ENV_DATA") == "1":
            self.report["retained_datasets"] = self.datasets
            self.save()
            return
        for base in reversed(self.datasets):
            snapshot = self.snapshot(base)
            documents = self.json("GET", base + "/documents")
            for document in documents["items"]:
                self.json("DELETE", base + "/documents/" + document["id"])
            assert self.client.delete(base).status_code == 204
            for collection in snapshot["collections"]:
                self.vector_store.drop_collection(collection)


@pytest.fixture
def real(request):
    if os.getenv("RUN_REAL_ENV") != "1":
        pytest.skip("set RUN_REAL_ENV=1 after starting real local models and infrastructure")
    with httpx.Client(base_url=os.getenv("API_E2E_URL", "http://127.0.0.1:8000"),
                      timeout=180, trust_env=False) as client:
        target = RealEnvironment(client, request.node.name)
        try:
            yield target
            target.report["status"] = "passed"
        finally:
            target.save()
            target.cleanup()


def test_real_high_quality_semantic_hybrid_revision_and_rebuild(real):
    base = real.dataset("高质量索引")
    documents = {}
    for name, content in {
        "fruit.txt": "苹果富含膳食纤维和多种维生素，是适合日常食用的健康水果。",
        "backup.txt": "数据库每天执行完整备份，并将备份副本保存在异地，以便灾难恢复。",
        "plane.txt": "飞机依靠喷气发动机产生推力，机翼在飞行中提供升力。",
    }.items():
        documents[name] = real.upload(base, name, content.encode())
    real.configure(base, list(documents.values()))
    snapshot = real.snapshot(base)
    assert snapshot["dimension"] == 1024
    assert snapshot["vector_count"] == len(snapshot["segments"]) == 3
    assert all(row["embedding_status"] == "completed" and row["keywords"] for row in snapshot["segments"])
    assert real.retrieve(base, "适合补充维他命的健康食物有哪些？", "vector")[0]["document_id"] == documents["fruit.txt"]
    assert real.retrieve(base, "发生灾害后怎样找回业务数据？", "vector")[0]["document_id"] == documents["backup.txt"]
    assert real.retrieve(base, "苹果 维生素", "full_text")[0]["document_id"] == documents["fruit.txt"]
    assert real.retrieve(base, "苹果 维生素", "hybrid")[0]["match_type"] == "hybrid"
    invalid = real.json("POST", base + "/retrieve", 422,
                        json={"query": "测试" * 300, "mode": "vector"})
    assert invalid["detail"] == "Embedding input is invalid."
    stream = real.client.post(base + "/retrieve/stream", json={"query": "测试" * 300, "mode": "vector"})
    assert stream.status_code == 200 and '"code":"RETRIEVAL_INPUT_INVALID"' in stream.text

    doc = base + "/documents/" + documents["fruit.txt"]
    segments = real.json("GET", doc + "/segments")
    segments["items"][0]["content"] += "\n修订标记 REALREVISION2026。"
    segments["items"][0]["keywords"] = []  # Request regeneration from the changed text.
    revision = real.json("PATCH", doc + "/segments", 202,
                         json={"base_version": segments["revision"], "segments": segments["items"]})
    real.wait(base, revision["job_id"])
    hits = real.retrieve(base, "REALREVISION2026", "full_text")
    assert hits and "REALREVISION2026" in hits[0]["content"]
    rebuilt = real.json("POST", base + "/rebuild", 202)
    real.wait(base, rebuilt["job_id"])
    new_snapshot = real.snapshot(base)
    assert new_snapshot["index_id"] != snapshot["index_id"]
    assert new_snapshot["vector_count"] == 3
    assert "REALREVISION2026" not in real.retrieve(base, "维他命", "vector")[0]["content"]


def test_real_parent_child_vectors_return_full_parent_context(real):
    base = real.dataset("父子分段")
    text = ("数据库备份方案：每天执行完整备份，将数据副本保存在异地，确保灾难发生后可以恢复业务。\n"
            "备份系统需要定期校验副本的完整性，并演练恢复步骤，记录恢复时间与数据损失范围。\n"
            "定期演练可以帮助团队发现问题，提高业务连续性，防止数据库故障导致长时间停机。\n"
            "文档结尾标记 FULLCONTEXT2026。")
    document = real.upload(base, "parent-context.txt", text.encode())
    real.configure(base, [document], {"mode": "parent_child", "parent_mode": "full_document",
                                    "parent_max_chunk_length": 1000, "child_max_chunk_length": 70,
                                    "child_overlap": 10})
    snapshot = real.snapshot(base)
    parents = [row for row in snapshot["segments"] if row["index_type"] == "parent"]
    children = [row for row in snapshot["segments"] if row["index_type"] == "child"]
    assert len(parents) == 1 and len(children) > 1
    assert snapshot["vector_count"] == len(children)
    assert parents[0]["embedding_status"] == "not_required"
    assert all(row["embedding_status"] == "completed" and row["keywords"] for row in children)
    for mode in ["vector", "full_text", "hybrid"]:
        hits = real.retrieve(base, "异地备份 灾难恢复", mode)
        assert len(hits) == 1
        assert hits[0]["parent_id"] == parents[0]["id"]
        assert "FULLCONTEXT2026" in hits[0]["content"]


def test_real_cpu_ocr_images_scanned_and_mixed_pdf(real):
    base = real.dataset("真实CPU OCR")
    documents = {}
    originals = {}
    for name in ["ocr-sample.png", "ocr-sample.jpg", "ocr-scan.pdf", "ocr-mixed.pdf"]:
        originals[name] = (FIXTURES / name).read_bytes()
        documents[name] = real.upload(base, name, originals[name], "application/octet-stream")
    real.configure(base, list(documents.values()))
    snapshot = real.snapshot(base)
    assert snapshot["dimension"] == 1024
    hits = real.retrieve(base, "ALPHA2026", "full_text", top_k=20)
    assert set(documents.values()) <= {item["document_id"] for item in hits}
    for name, document in documents.items():
        doc = base + "/documents/" + document
        segments = real.json("GET", doc + "/segments")["items"]
        content = "\n".join(row["content"] for row in segments)
        assert "文档解析与清洗测试" in content
        assert "OCRTEST123" in content.replace(" ", "")
        assert "ALPHA2026" in content
        ocr = [row for row in segments if row["source_metadata"].get("ocr")]
        assert ocr and all(row["source_metadata"].get("confidence", 0) > 0.8 for row in ocr)
        assert all(row["source_metadata"].get("boxes") for row in ocr)
        if name == "ocr-mixed.pdf":
            assert "Alphabeta" in content
        download = real.client.get(doc + "/download")
        assert download.status_code == 200
        assert hashlib.sha256(download.content).digest() == hashlib.sha256(originals[name]).digest()
    assert {item["document_id"] for item in real.retrieve(base, "项目编号和金额", "vector", top_k=20)} == set(documents.values())


def test_real_graph_extraction_neo4j_neighbor_retrieval_and_visibility(real):
    if os.getenv("RUN_REAL_GRAPH") != "1":
        pytest.skip("set RUN_REAL_GRAPH=1 after enabling the actual extraction model and Neo4j")
    base = real.dataset("Neo4j实体关系")
    first = real.upload(base, "cooperation.txt", "星河科技与远山研究院合作。".encode())
    second = real.upload(base, "research.txt", "远山研究院研发智能检索系统。".encode())
    unrelated = real.upload(base, "unrelated.txt", "海风书店位于青岛。".encode())
    real.configure(base, [first, second, unrelated])
    hits = real.retrieve(base, "星河科技有哪些合作机构？", "graph")
    assert {item["document_id"] for item in hits} == {first, second}
    assert all(item["match_type"] == "graph" for item in hits)
    assert any(item["document_id"] == second and "星河科技" not in item["content"] for item in hits)
    real.json("POST", base + "/documents/" + second + "/disable")
    assert second not in {item["document_id"] for item in real.retrieve(base, "星河科技有哪些合作机构？", "graph")}
    real.json("POST", base + "/documents/" + second + "/enable")
    assert second in {item["document_id"] for item in real.retrieve(base, "星河科技有哪些合作机构？", "graph")}


def test_real_permanent_embedding_input_error_fails_without_retry(real):
    base = real.dataset("模型输入限制")
    document = real.upload(base, "overlong.txt", ("输入长度限制测试。" * 75).encode())
    config = {"document_ids": [document], "indexing_technique": "high_quality",
              "embedding_model": real.model,
              "segmentation": {"mode": "general", "max_chunk_length": 700, "overlap": 0}}
    job = real.json("POST", base + "/indexing/configure", 202, json=config)
    result = real.wait(base, job["job_id"], expected="failed")
    assert result["documents"][0]["error_code"] == "EMBEDDING_INPUT_INVALID"
    assert result["documents"][0]["attempt"] == 1
    assert real.snapshot(base)["index_id"] is None
    real.json("DELETE", base + "/documents/" + document)
    assert real.client.delete(base).status_code == 204
    real.datasets.remove(base)
