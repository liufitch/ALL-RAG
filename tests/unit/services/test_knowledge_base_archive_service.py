import hashlib
import io
import json
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from rag_modules.db.models import DatasetRecord, DocumentRecord
from rag_modules.services.knowledge_base_archive_service import KnowledgeBaseArchiveService


class Repo:
    async def get_active(self, dataset_id):
        return DatasetRecord(
            id=dataset_id, name="知识库", description="说明", provider="vendor",
            permission="only_me", indexing_technique="high_quality", created_by="u1",
            created_at=datetime.now(timezone.utc), retrieval_model_config={"top_k": 5},
        )

    async def list_all_documents(self, dataset_id):
        return [DocumentRecord(
            id="doc-1", dataset_id=dataset_id, position=1,
            data_source_type="upload_file", data_source_info={
                "object_key": "datasets/dataset-1/documents/doc-1/source.txt",
                "content_type": "text/plain", "size": 5, "sha256": hashlib.sha256(b"hello").hexdigest(),
            }, name="guide.txt", created_from="api", created_by="u1", indexing_status="completed",
        )]


class Storage:
    @asynccontextmanager
    async def get_stream(self, object_key):
        yield io.BytesIO(b"hello")


@pytest.mark.asyncio
async def test_export_contains_manifest_metadata_and_file(tmp_path):
    path = await KnowledgeBaseArchiveService(Repo(), Storage(), temp_dir=tmp_path).export_dataset("dataset-1")

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        assert {"manifest.json", "dataset.json", "retrieval.json", "documents.json"}.issubset(names)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format_version"] == 1
        assert manifest["files"][0]["sha256"] == hashlib.sha256(b"hello").hexdigest()
        assert archive.read("files/doc-1/guide.txt") == b"hello"
