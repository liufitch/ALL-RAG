import hashlib
import io
import json
import zipfile

import pytest

from rag_modules.services.knowledge_base_archive_service import ArchiveValidationError, KnowledgeBaseArchiveService


def archive_bytes(path="files/doc-1/a.txt", content=b"hello", digest=None):
    digest = digest or hashlib.sha256(content).hexdigest()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format_version": 1, "source_dataset_id": "old", "document_count": 1, "files": [{"path": path, "sha256": digest, "size": len(content)}]}))
        archive.writestr("dataset.json", json.dumps({"name": "导入库", "description": "说明", "permission": "only_me", "indexing_technique": "high_quality"}))
        archive.writestr("retrieval.json", json.dumps({"mode": "hybrid", "top_k": 5, "score_threshold": 0.3, "semantic_weight": 0.7, "keyword_weight": 0.3, "rerank_enabled": False}))
        archive.writestr("documents.json", json.dumps([{"id": "doc-1", "name": "a.txt", "position": 1, "data_source_type": "upload_file", "data_source_info": {"content_type": "text/plain"}, "archive_path": path}]))
        archive.writestr(path, content)
    return stream.getvalue()


class Repo:
    def __init__(self):
        self.payload = None

    async def create_import_records(self, **payload):
        self.payload = payload
        return payload["dataset_id"]


class Storage:
    def __init__(self):
        self.keys = []

    async def put_stream(self, object_key, stream, length, content_type):
        self.keys.append(object_key)

    async def remove_object(self, object_key):
        self.keys.remove(object_key)


@pytest.mark.asyncio
async def test_import_creates_new_ids_and_uploads_original_file(tmp_path):
    repo, storage = Repo(), Storage()
    result = await KnowledgeBaseArchiveService(repo, storage, temp_dir=tmp_path).import_dataset(io.BytesIO(archive_bytes()))

    assert result["dataset_id"] != "old"
    assert repo.payload["documents"][0]["status"] == "waiting"
    assert storage.keys[0].startswith(f"datasets/{result['dataset_id']}/documents/")


@pytest.mark.asyncio
async def test_import_rejects_path_traversal():
    with pytest.raises(ArchiveValidationError):
        await KnowledgeBaseArchiveService(Repo(), Storage()).import_dataset(io.BytesIO(archive_bytes("files/../secret.txt")))


@pytest.mark.asyncio
async def test_import_rejects_hash_mismatch():
    with pytest.raises(ArchiveValidationError):
        await KnowledgeBaseArchiveService(Repo(), Storage()).import_dataset(io.BytesIO(archive_bytes(digest="0" * 64)))
