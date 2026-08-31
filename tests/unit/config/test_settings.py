import pytest

from rag_modules.config.settings import Settings


def test_nested_embedding_catalog_and_secrets_are_loaded(monkeypatch):
    """Removing nested embedding support would break environment configuration."""
    monkeypatch.setenv("EMBEDDING__BASE_URL", "http://embed:8000/v1")
    monkeypatch.setenv("EMBEDDING__API_KEY", "secret-value")
    monkeypatch.setenv("EMBEDDING__DEFAULT_MODEL", "bge-m3")
    monkeypatch.setenv(
        "EMBEDDING__MODELS",
        '[{"id":"bge-m3","model":"BAAI/bge-m3","display_name":"BGE-M3"}]',
    )

    loaded = Settings(_env_file=None)

    assert loaded.embedding.get_model("bge-m3").model == "BAAI/bge-m3"
    assert loaded.embedding.api_key.get_secret_value() == "secret-value"
    assert "secret-value" not in repr(loaded.embedding)


def test_default_embedding_model_must_be_enabled(monkeypatch):
    """Accepting an absent default would defer a configuration error until indexing."""
    monkeypatch.setenv("EMBEDDING__DEFAULT_MODEL", "missing")
    monkeypatch.setenv("EMBEDDING__MODELS", "[]")

    with pytest.raises(ValueError, match="default embedding model"):
        Settings(_env_file=None)


def test_indexing_runtime_settings_are_available_with_approved_defaults():
    """Changing approved segmentation defaults would change later indexing behavior."""
    loaded = Settings(_env_file=None)

    assert loaded.object_storage.bucket == "graph-rag-uploads"
    assert loaded.broker.url.startswith("amqp://")
    assert loaded.upload.max_file_size_mb > 0
    assert loaded.parser.max_pdf_pages > 0
    assert loaded.preview.timeout_seconds > 0
    assert loaded.indexing.default_indexing_technique == "high_quality"
    assert loaded.indexing.general_max_chunk_length == 1024
    assert loaded.indexing.general_overlap == 100
    assert loaded.indexing.parent_max_chunk_length == 4096
    assert loaded.indexing.child_max_chunk_length == 512
    assert loaded.indexing.child_overlap == 50
