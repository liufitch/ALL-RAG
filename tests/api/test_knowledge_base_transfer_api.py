import io
from pathlib import Path

import pytest

from main import app
from rag_modules.api.knowledge_base_settings_api import get_archive_service
from rag_modules.services.knowledge_base_archive_service import ArchiveValidationError


class ArchiveStub:
    async def export_dataset(self, dataset_id):
        path = Path("/tmp/knowledge-base-test-export.zip")
        path.write_bytes(b"PK-test")
        return str(path)

    async def import_dataset(self, fileobj):
        assert fileobj.read(3) == b"PK-"
        return {"dataset_id": "new-id", "document_count": 1}


@pytest.fixture
def transfer_client(client):
    app.dependency_overrides[get_archive_service] = lambda: ArchiveStub()
    yield client
    app.dependency_overrides.clear()


def test_export_returns_zip_download(transfer_client):
    response = transfer_client.get("/api/knowledge_base/dataset-1/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert "knowledge-base-dataset-1.zip" in response.headers["content-disposition"]


def test_import_returns_created_dataset(transfer_client):
    response = transfer_client.post(
        "/api/knowledge_base/import",
        files={"file": ("backup.zip", io.BytesIO(b"PK-test"), "application/zip")},
    )
    assert response.status_code == 201
    assert response.json() == {"dataset_id": "new-id", "document_count": 1}
