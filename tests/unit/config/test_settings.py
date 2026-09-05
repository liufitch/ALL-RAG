import pytest
import importlib.util
import sys
from pathlib import Path

from rag_modules.config.settings import (
    DatabaseSettings,
    ParserSettings,
    PreviewSettings,
    Settings,
    UploadSettings,
    VectorStoreSettings,
)


def test_project_env_loads_credentials_with_legacy_fallback_and_environment_priority(tmp_path, monkeypatch):
    import rag_modules.config.settings as source

    # 用独立目录验证默认查找路径，不读取或修改开发者的真实 .env。
    config_dir = tmp_path / "rag_modules" / "config"
    config_dir.mkdir(parents=True)
    module_path = config_dir / "settings.py"
    module_path.write_text(Path(source.__file__).read_text())
    (config_dir / ".env").write_text("OBJECT_STORAGE__ACCESS_KEY=legacy-key\nOBJECT_STORAGE__SECRET_KEY=legacy-secret\n")
    (tmp_path / ".env").write_text("OBJECT_STORAGE__ACCESS_KEY=project-key\n")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("OBJECT_STORAGE__ACCESS_KEY", raising=False)
    monkeypatch.delenv("OBJECT_STORAGE__SECRET_KEY", raising=False)
    spec = importlib.util.spec_from_file_location("settings_probe", module_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "settings_probe", module)
    spec.loader.exec_module(module)
    loaded = module.Settings()
    assert loaded.object_storage.access_key.get_secret_value() == "project-key"
    assert loaded.object_storage.secret_key.get_secret_value() == "legacy-secret"
    monkeypatch.setenv("OBJECT_STORAGE__ACCESS_KEY", "environment-key")
    assert module.Settings().object_storage.access_key.get_secret_value() == "environment-key"


def test_nested_embedding_catalog_and_secrets_are_loaded(monkeypatch):
    """移除嵌套嵌入配置支持会破坏环境变量配置。"""
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
    """接受不存在的默认项，会把配置错误推迟到建立索引时才暴露。"""
    monkeypatch.setenv("EMBEDDING__DEFAULT_MODEL", "missing")
    monkeypatch.setenv("EMBEDDING__MODELS", "[]")

    with pytest.raises(ValueError, match="default embedding model"):
        Settings(_env_file=None)


def test_indexing_runtime_settings_are_available_with_approved_defaults():
    """修改约定的分段默认值会改变后续索引行为。"""
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


def test_vector_store_batching_and_consistency_defaults_are_bounded():
    vector_store = VectorStoreSettings()

    assert vector_store.batch_size == 500
    assert vector_store.consistency_poll_attempts == 5
    assert vector_store.consistency_poll_interval_seconds == 0.05
    assert vector_store.connect_timeout == 5


def test_database_engine_options_always_hide_bound_parameters():
    sqlite_options = DatabaseSettings(type="sqlite", echo=True).engine_options
    postgresql_options = DatabaseSettings(
        type="postgresql",
        host="database.internal",
        port=5432,
        echo=True,
        pool_size=7,
        max_overflow=11,
        pool_timeout=13,
        pool_recycle=17,
    ).engine_options

    assert sqlite_options == {"echo": True, "hide_parameters": True}
    assert postgresql_options == {
        "echo": True,
        "hide_parameters": True,
        "pool_size": 7,
        "max_overflow": 11,
        "pool_timeout": 13,
        "pool_recycle": 17,
    }


def test_application_async_engine_is_configured_to_hide_bound_parameters():
    from rag_modules.db.session import engine

    assert engine.sync_engine.hide_parameters is True


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("batch_size", 0),
        ("batch_size", 10_001),
        ("consistency_poll_attempts", 1),
        ("consistency_poll_attempts", 101),
        ("consistency_poll_interval_seconds", -0.01),
        ("consistency_poll_interval_seconds", 5.01),
        ("connect_timeout", 0),
        ("connect_timeout", 121),
    ),
)
def test_vector_store_runtime_bounds_reject_invalid_values(field_name, value):
    with pytest.raises(ValueError):
        VectorStoreSettings(**{field_name: value})


def test_upload_settings_reject_extensions_outside_approved_formats():
    with pytest.raises(ValueError, match=r"unsupported upload extensions: \.exe"):
        UploadSettings(allowed_extensions=(".txt", ".exe"))


def test_spreadsheet_merged_cell_limits_have_approved_defaults():
    """修改任一合并单元格限制，都可能在无提示的情况下改变可接受的 XLSX 处理范围。"""
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
    """非正数的合并限制会使解析器契约失去合理含义。"""
    with pytest.raises(ValueError):
        ParserSettings(**{field_name: 0})


def test_total_merged_cell_limit_cannot_be_smaller_than_single_range_limit():
    """允许总量上限低于单个合法范围，会产生相互矛盾的限制。"""
    with pytest.raises(ValueError, match="total merged-cell area"):
        ParserSettings(
            max_physical_cells=10,
            max_merged_cell_area=5,
            max_total_merged_cell_area=4,
        )


def test_total_merged_cell_limit_cannot_exceed_physical_cell_limit():
    """合并单元格展开必须受解析器总体实体化数量上限约束。"""
    with pytest.raises(ValueError, match="total merged-cell area"):
        ParserSettings(
            max_physical_cells=5,
            max_merged_cell_area=5,
            max_total_merged_cell_area=6,
        )
