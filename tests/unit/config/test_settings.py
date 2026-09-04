import pytest

from rag_modules.config.settings import (
    ParserSettings,
    PreviewSettings,
    Settings,
    UploadSettings,
)


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


def test_upload_settings_reject_extensions_outside_approved_formats():
    with pytest.raises(ValueError, match=r"unsupported upload extensions: \.exe"):
        UploadSettings(allowed_extensions=(".txt", ".exe"))


def test_spreadsheet_merged_cell_limits_have_approved_defaults():
    """Changing either merge bound can silently alter accepted XLSX work."""
    parser = ParserSettings()

    assert parser.max_merged_cell_area == 100_000
    assert parser.max_total_merged_cell_area == 1_000_000


def test_warning_limits_have_approved_defaults():
    parser = ParserSettings()
    preview = PreviewSettings()

    assert parser.max_warnings_per_document == 100
    assert parser.max_formula_warning_samples == 5
    assert preview.max_warnings == 100


@pytest.mark.parametrize(
    ("settings_type", "field_name", "value"),
    (
        (ParserSettings, "max_warnings_per_document", 0),
        (ParserSettings, "max_formula_warning_samples", 0),
        (ParserSettings, "max_formula_warning_samples", 101),
        (PreviewSettings, "max_warnings", 0),
    ),
)
def test_warning_limits_reject_values_outside_their_safe_bounds(
    settings_type, field_name, value
):
    with pytest.raises(ValueError):
        settings_type(**{field_name: value})


@pytest.mark.parametrize(
    "field_name", ("max_merged_cell_area", "max_total_merged_cell_area")
)
def test_spreadsheet_merged_cell_limits_must_be_positive(field_name):
    """A non-positive merge bound would make the parser contract nonsensical."""
    with pytest.raises(ValueError):
        ParserSettings(**{field_name: 0})


def test_total_merged_cell_limit_cannot_be_smaller_than_single_range_limit():
    """Allowing the total below one legal range creates contradictory limits."""
    with pytest.raises(ValueError, match="total merged-cell area"):
        ParserSettings(
            max_physical_cells=10,
            max_merged_cell_area=5,
            max_total_merged_cell_area=4,
        )


def test_total_merged_cell_limit_cannot_exceed_physical_cell_limit():
    """Merge expansion must stay within the parser's overall materialization cap."""
    with pytest.raises(ValueError, match="total merged-cell area"):
        ParserSettings(
            max_physical_cells=5,
            max_merged_cell_area=5,
            max_total_merged_cell_area=6,
        )
