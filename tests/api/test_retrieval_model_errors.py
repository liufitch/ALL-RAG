import pytest

from main import app
from rag_modules.api import retrieval_api
from rag_modules.embeddings import EmbeddingError
from tests.api.test_retrieval_stream_api import DB


@pytest.mark.parametrize(("code", "retryable", "status"), [
    ("EMBEDDING_INPUT_INVALID", False, 422),
    ("EMBEDDING_AUTH_FAILED", False, 503),
    ("EMBEDDING_REQUEST_FAILED", True, 503),
])
def test_retrieval_returns_safe_model_errors_instead_of_500(client, code, retryable, status):
    class Service:
        async def retrieve(self, *args, **kwargs):
            raise EmbeddingError(code, retryable, "Safe model error.")

    app.dependency_overrides[retrieval_api.get_retrieval_service] = lambda: Service()
    app.dependency_overrides[retrieval_api.get_db_session] = lambda: DB()
    response = client.post("/api/knowledge_base/dataset-1/retrieve", json={"query": "test"})

    assert response.status_code == status
    assert response.json()["detail"] == "Safe model error."


def test_stream_identifies_model_input_rejection_as_invalid_input(client):
    class Service:
        async def retrieve(self, *args, **kwargs):
            raise EmbeddingError("EMBEDDING_INPUT_INVALID", False, "Embedding input is invalid.")

    app.dependency_overrides[retrieval_api.get_retrieval_service] = lambda: Service()
    app.dependency_overrides[retrieval_api.get_db_session] = lambda: DB()
    response = client.post("/api/knowledge_base/dataset-1/retrieve/stream", json={"query": "test"})

    assert response.status_code == 200
    assert '"code":"RETRIEVAL_INPUT_INVALID"' in response.text
    assert "event: hit" not in response.text and "event: done" not in response.text
