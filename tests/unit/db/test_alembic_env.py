from __future__ import annotations

from contextlib import nullcontext
import importlib
from pathlib import Path
import runpy
from types import SimpleNamespace
import tomllib

from alembic import context
from alembic.config import Config
import sqlalchemy.ext.asyncio

from rag_modules.config.settings import DatabaseSettings

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_runtime_declares_sqlalchemy_asyncio_extra():
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    dependencies = {dependency.lower() for dependency in project["project"]["dependencies"]}

    assert "sqlalchemy[asyncio]>=2.0.0" in dependencies


def test_alembic_env_escapes_encoded_database_url_for_config_parser(monkeypatch):
    database = DatabaseSettings(
        type="postgresql",
        host="db.internal",
        port=5432,
        database="graph_rag",
        username="migration-user",
        password="fixture@%/value",
    )
    database_uri = database.uri
    alembic_config = Config()
    configured: dict[str, object] = {}
    settings_module = importlib.import_module("rag_modules.config.settings")

    monkeypatch.setattr(
        settings_module,
        "settings",
        SimpleNamespace(sqlalchemy_database_uri=database_uri),
    )
    monkeypatch.setattr(context, "config", alembic_config, raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: True)
    monkeypatch.setattr(context, "configure", lambda **kwargs: configured.update(kwargs))
    monkeypatch.setattr(context, "begin_transaction", nullcontext)
    monkeypatch.setattr(context, "run_migrations", lambda: None)

    runpy.run_path(
        str(PROJECT_ROOT / "migrations" / "env.py"),
        run_name="__test_alembic_env__",
    )

    assert alembic_config.get_main_option("sqlalchemy.url") == database_uri
    assert configured["url"] == database_uri


def test_online_migration_engine_hides_bound_parameters(monkeypatch):
    alembic_config = Config()
    engine_arguments: dict[str, object] = {}

    class FakeConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def run_sync(self, _operation):
            return None

    class FakeEngine:
        def connect(self):
            return FakeConnection()

        async def dispose(self):
            return None

    def fake_async_engine_from_config(configuration, **kwargs):
        engine_arguments.update(kwargs)
        return FakeEngine()

    monkeypatch.setattr(
        sqlalchemy.ext.asyncio,
        "async_engine_from_config",
        fake_async_engine_from_config,
    )
    monkeypatch.setattr(
        "rag_modules.config.settings.settings",
        SimpleNamespace(sqlalchemy_database_uri="sqlite+aiosqlite:///:memory:"),
    )
    monkeypatch.setattr(context, "config", alembic_config, raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: False)

    runpy.run_path(
        str(PROJECT_ROOT / "migrations" / "env.py"),
        run_name="__test_online_alembic_env__",
    )

    assert engine_arguments["hide_parameters"] is True
