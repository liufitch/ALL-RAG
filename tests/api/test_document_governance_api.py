from types import SimpleNamespace

from rag_modules.api.file_api import get_document_service
from rag_modules.services.document_service import DocumentUploadItem
from main import app


class GovernanceStub:
    def __init__(self):
        self.document = SimpleNamespace(
            id="doc-1", dataset_id="dataset-1", name="guide.txt", indexing_status="completed",
            enabled=True, archived=False, updated_at=None, error=None,
            data_source_info={"object_key": "documents/doc-1/source.txt", "content_type": "text/plain", "size": 5},
        )
        self.storage = SimpleNamespace()

    async def get_document(self, dataset_id, document_id):
        if dataset_id != "dataset-1" or document_id != "doc-1":
            from rag_modules.services.document_service import DocumentNotFoundError
            raise DocumentNotFoundError(document_id)
        return self.document

    async def rename_document(self, dataset_id, document_id, name, actor_id):
        self.document.name = name
        self.document.data_source_info["original_filename"] = name
        return self.document

    async def set_enabled(self, *args):
        self.document.enabled = args[2]
        return self.document

    async def set_archived(self, *args):
        self.document.archived = args[2]
        return self.document

    async def delete_document(self, *args):
        return self.document

    async def list_segments(self, *args):
        return []


def test_document_governance_actions_return_document_contract(client):
    service = GovernanceStub()
    app.dependency_overrides[get_document_service] = lambda: service
    try:
        renamed = client.patch(
            "/api/knowledge_base/dataset-1/documents/doc-1",
            json={"name": "renamed.txt"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["document"]["name"] == "renamed.txt"

        disabled = client.post(
            "/api/knowledge_base/dataset-1/documents/doc-1/disable"
        )
        assert disabled.status_code == 200
        assert disabled.json()["document"]["enabled"] is False

        segments = client.get(
            "/api/knowledge_base/dataset-1/documents/doc-1/segments"
        )
        assert segments.status_code == 200
        assert segments.json()["items"] == []
    finally:
        app.dependency_overrides.clear()
