from __future__ import annotations

from types import SimpleNamespace

import pytest

from rag_modules.services.document_service import (
    DocumentNotFoundError,
    DocumentService,
    DocumentValidationError,
)


class Repository:
    def __init__(self, record=None):
        self.record = record
        self.calls = []

    async def get_active_document(self, dataset_id, document_id):
        return self.record

    async def update_name(self, *args):
        self.calls.append(("rename", args))
        if self.record is None:
            return None
        self.record.name = args[2]
        return self.record

    async def set_enabled(self, *args):
        self.calls.append(("enabled", args))
        if self.record is None:
            return None
        self.record.enabled = args[2]
        return self.record

    async def set_archived(self, *args):
        self.calls.append(("archived", args))
        if self.record is None:
            return None
        self.record.archived = args[2]
        return self.record

    async def soft_delete(self, *args):
        self.calls.append(("delete", args))
        if self.record is None:
            return None
        self.record.deleted_at = object()
        return self.record

    async def list_segments(self, *args):
        return ["segment"]


def service(repository):
    return DocumentService(
        repository=repository,
        dataset_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
        upload_settings=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_governance_service_validates_name_and_delegates_mutations():
    record = SimpleNamespace(name="old.txt", enabled=True, archived=False, deleted_at=None)
    repository = Repository(record)
    target = service(repository)

    await target.rename_document("dataset-1", "doc-1", "  new.txt ", "user-1")
    await target.set_enabled("dataset-1", "doc-1", False, "user-1")
    await target.set_archived("dataset-1", "doc-1", True, "user-1")
    await target.delete_document("dataset-1", "doc-1")

    assert record.name == "new.txt"
    assert record.enabled is False
    assert record.archived is True
    assert record.deleted_at is not None

    with pytest.raises(DocumentValidationError):
        await target.rename_document("dataset-1", "doc-1", "   ", "user-1")


@pytest.mark.asyncio
async def test_governance_service_rejects_missing_document():
    target = service(Repository(None))

    with pytest.raises(DocumentNotFoundError):
        await target.set_enabled("dataset-1", "missing", True, "user-1")
