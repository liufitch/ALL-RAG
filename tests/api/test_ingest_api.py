from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

from rag_modules.api.ingest_api import ingest_documents
from rag_modules.services.document_service import DocumentUploadItem


class FakeDocumentService:
    def __init__(self, items=None, *, fail_at=None):
        self.items = list(items or [])
        self.fail_at = fail_at
        self.upload_calls = []
        self.compensated = []

    async def upload_one(self, dataset_id, file, actor_id):
        self.upload_calls.append(file.filename)
        if self.fail_at == len(self.upload_calls):
            raise RuntimeError("upload failed")
        return self.items[len(self.upload_calls) - 1]

    async def compensate_upload(self, dataset_id, document_id):
        self.compensated.append((dataset_id, document_id))


class FakeRuleService:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    async def confirm(self, dataset_id, request, actor_id):
        self.calls.append((dataset_id, request, actor_id))
        if self.error:
            raise self.error
        return SimpleNamespace(
            rule=SimpleNamespace(id="rule-1"),
            job=SimpleNamespace(id="job-1"),
            job_documents=[],
        )


class FakePublisher:
    def dispatch_job_document(self, *, job_document_id):
        return job_document_id


def item(identifier, *, duplicate=False):
    return DocumentUploadItem(
        id=identifier,
        dataset_id="dataset-1",
        name=f"{identifier}.txt",
        status="waiting",
        duplicate=duplicate,
    )


@pytest.mark.asyncio
async def test_invalid_ingest_configuration_is_rejected_before_upload():
    documents = FakeDocumentService([item("doc-1")])
    rules = FakeRuleService()

    with pytest.raises(HTTPException) as error:
        await ingest_documents(
            "dataset-1", [UploadFile(filename="a.txt", file=__import__("io").BytesIO(b"a"))],
            json.dumps({"segmentation": {"mode": "general", "max_chunk_length": 0}}),
            documents, rules, FakePublisher(),
        )

    assert error.value.status_code == 422
    assert documents.upload_calls == []
    assert rules.calls == []


@pytest.mark.asyncio
async def test_rule_failure_compensates_only_new_documents():
    documents = FakeDocumentService([item("doc-1"), item("doc-existing", duplicate=True)])
    rules = FakeRuleService(error=RuntimeError("database failed"))
    files = [
        UploadFile(filename="a.txt", file=__import__("io").BytesIO(b"a")),
        UploadFile(filename="b.txt", file=__import__("io").BytesIO(b"b")),
    ]

    with pytest.raises(RuntimeError, match="database failed"):
        await ingest_documents(
            "dataset-1", files,
            json.dumps({"segmentation": {"mode": "general"}}),
            documents, rules, FakePublisher(),
        )

    assert documents.compensated == [("dataset-1", "doc-1")]


@pytest.mark.asyncio
async def test_upload_failure_compensates_prior_new_documents():
    documents = FakeDocumentService([item("doc-1"), item("unused")], fail_at=2)
    files = [
        UploadFile(filename="a.txt", file=__import__("io").BytesIO(b"a")),
        UploadFile(filename="b.txt", file=__import__("io").BytesIO(b"b")),
    ]

    with pytest.raises(RuntimeError, match="upload failed"):
        await ingest_documents(
            "dataset-1", files,
            json.dumps({"segmentation": {"mode": "general"}}),
            documents, FakeRuleService(), FakePublisher(),
        )

    assert documents.compensated == [("dataset-1", "doc-1")]
