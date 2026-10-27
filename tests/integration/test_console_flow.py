"""Opt-in HTTP checks against an API with PostgreSQL, MinIO and a live Worker.

RUN_API_E2E=1 API_E2E_URL=http://127.0.0.1:8000 python -m pytest \
    tests/integration/test_console_flow.py -v -s
"""

import io
import json
import os
import time
import zipfile
from urllib.parse import unquote
from uuid import uuid4

import httpx
import pytest


@pytest.fixture
def live_api():
    if os.getenv("RUN_API_E2E") != "1":
        pytest.skip("set RUN_API_E2E=1 to test the live API and Worker")
    with httpx.Client(
        base_url=os.getenv("API_E2E_URL", "http://127.0.0.1:8000"),
        timeout=30, trust_env=False,
    ) as client:
        yield client


def _json(client, method, path, status=200, **kwargs):
    response = client.request(method, path, **kwargs)
    assert response.status_code == status, (method, path, response.status_code, response.text)
    return response.json()


def _wait_job(client, base, job_id):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = _json(client, "GET", f"{base}/indexing/jobs/{job_id}")
        if job["status"] in {"completed", "failed", "cancelled", "partially_completed"}:
            assert job["status"] == "completed", job
            return
        time.sleep(0.5)
    pytest.fail(f"Worker did not finish job {job_id} within 120s")


@pytest.mark.integration
@pytest.mark.e2e
def test_live_document_lifecycle_and_archive_round_trip(live_api):
    client = live_api
    datasets = []
    token = "FUNCTIONAL" + uuid4().hex.upper()
    content = f"知识库功能验证 {token} document indexing and retrieval.".encode()
    name = "中文功能验证 📄.txt"
    try:
        dataset = _json(client, "POST", "/api/knowledge_base", 201, json={
            "name": f"自动化功能验证 {token}", "permission": "only_me",
        })
        base = "/api/knowledge_base/" + dataset["id"]
        datasets.append(base)
        upload = _json(client, "POST", base + "/documents/upload", 201,
                       files={"files": (name, content, "text/plain")})
        assert not upload["rejected"]
        document = upload["documents"][0]
        doc = base + "/documents/" + document["id"]
        duplicate = _json(client, "POST", base + "/documents/upload", 201,
                          files={"files": (name, content, "text/plain")})
        assert duplicate["documents"][0]["duplicate"] is True
        assert duplicate["documents"][0]["id"] == document["id"]
        download = client.get(doc + "/download")
        assert download.status_code == 200
        assert download.content == content
        assert unquote(download.headers["content-disposition"].split("filename*=utf-8''")[1]) == name

        config = {"document_ids": [document["id"]], "indexing_technique": "economy",
                  "embedding_model": None,
                  "segmentation": {"mode": "general", "max_chunk_length": 500, "overlap": 50}}
        preview = _json(client, "POST", base + "/indexing/preview", json=config)
        assert token in preview["chunks"][0]["content"]
        job = _json(client, "POST", base + "/indexing/configure", 202, json=config)
        _wait_job(client, base, job["job_id"])
        settings = _json(client, "PATCH", base + "/settings", json={
            "description": "真实 API 验证", "retrieval": {"mode": "full_text", "score_threshold": 0},
        })
        assert settings["indexing"]["technique"] == "economy"

        def hits():
            return _json(client, "POST", base + "/retrieve", json={"query": token})["items"]

        assert any(item["document_id"] == document["id"] for item in hits())
        sse = client.post(base + "/retrieve/stream", json={"query": token})
        assert sse.status_code == 200
        assert "event: hit" in sse.text and "event: done" in sse.text
        assert "event: error" not in sse.text
        for action, visible in [("disable", False), ("enable", True), ("archive", False), ("restore", True)]:
            _json(client, "POST", doc + "/" + action)
            assert bool(hits()) is visible, action

        renamed = _json(client, "PATCH", doc, json={"name": "重命名后的文档.txt"})
        assert renamed["document"]["name"] == "重命名后的文档.txt"
        segments = _json(client, "GET", doc + "/segments")
        assert segments["items"]
        segments["items"][0]["content"] += "\nMANUALFUNCTIONALREVISION"
        revision_payload = {"base_version": segments["revision"], "segments": segments["items"]}
        revision = _json(client, "PATCH", doc + "/segments", 202, json=revision_payload)
        _wait_job(client, base, revision["job_id"])
        stale = client.patch(doc + "/segments", json=revision_payload)
        assert stale.status_code == 409
        assert any("MANUALFUNCTIONALREVISION" in item["content"] for item in hits())
        assert client.get(doc + "/download").content == content

        exported = client.get(base + "/export")
        assert exported.status_code == 200
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            metadata = json.loads(archive.read("documents.json"))
            assert archive.read(metadata[0]["archive_path"]) == content
        imported = _json(client, "POST", "/api/knowledge_base/import?rebuild=true", 201,
                         files={"file": ("backup.zip", exported.content, "application/zip")})
        imported_base = "/api/knowledge_base/" + imported["dataset_id"]
        datasets.append(imported_base)
        assert imported["dataset_id"] != dataset["id"] and imported["document_count"] == 1
        imported_settings = _json(client, "GET", imported_base + "/settings")
        assert imported_settings["dataset"]["description"] == "真实 API 验证"
        assert imported_settings["indexing"] == settings["indexing"]
        assert imported_settings["retrieval"] == settings["retrieval"]
        _wait_job(client, imported_base, imported["job_id"])
        imported_hits = _json(client, "POST", imported_base + "/retrieve", json={"query": token})["items"]
        assert imported_hits and token in imported_hits[0]["content"]
        # Archives contain original files, so edits to derived segments do not replace the source.
        assert "MANUALFUNCTIONALREVISION" not in imported_hits[0]["content"]
        _json(client, "DELETE", doc)
        assert not hits()
        assert client.get(doc + "/download").status_code == 404
        print("Live upload/download/index/retrieve/SSE/governance/revision/export/import/delete passed")
    finally:
        # Remove only resources created by this test, leaving existing knowledge bases intact.
        for base in reversed(datasets):
            documents = _json(client, "GET", base + "/documents")
            for document in documents["items"]:
                _json(client, "DELETE", base + "/documents/" + document["id"])
            assert client.delete(base).status_code == 204


@pytest.mark.integration
@pytest.mark.parametrize("manifest", [[], {"format_version": 1, "document_count": 0, "files": [None]}])
def test_live_import_returns_validation_error_for_malformed_metadata(live_api, manifest):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, payload in {"manifest.json": manifest, "dataset.json": {"name": "invalid"},
                              "retrieval.json": {}, "documents.json": []}.items():
            archive.writestr(name, json.dumps(payload))
    response = live_api.post("/api/knowledge_base/import",
                             files={"file": ("invalid.zip", stream.getvalue(), "application/zip")})
    assert response.status_code == 422, response.text
