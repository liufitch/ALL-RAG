"""Exercise revision binding migration against an isolated PostgreSQL schema."""
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from rag_modules.config.settings import settings


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revision_input_migration_preserves_legacy_binding_and_enforces_fk():
    if os.getenv("RUN_INTEGRATION") != "1":
        pytest.skip("set RUN_INTEGRATION=1 to test PostgreSQL migration")
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[2] / "migrations/versions/20261116_02_revision_inputs.py"
    spec = importlib.util.spec_from_file_location("revision_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = "revision_test_" + uuid4().hex
    engine = create_async_engine(settings.sqlalchemy_database_uri, hide_parameters=True)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                for statement in [
                    "CREATE TABLE indexing_jobs (id varchar(36) PRIMARY KEY, dataset_id varchar(36), revision_id varchar(36))",
                    "CREATE TABLE document_revisions (id varchar(36) PRIMARY KEY, dataset_id varchar(36), document_id varchar(36))",
                    "CREATE TABLE indexing_job_documents (id varchar(36) PRIMARY KEY, job_id varchar(36), document_id varchar(36))",
                    "INSERT INTO indexing_jobs VALUES ('job','dataset','revision'), ('plain-job','dataset',NULL)",
                    "INSERT INTO document_revisions VALUES ('revision','dataset','document')",
                    "INSERT INTO indexing_job_documents VALUES ('legacy','job','document'), ('wrong-document','job','other-document'), ('plain','plain-job','document')",
                ]:
                    await conn.execute(text(statement))
                def upgrade(sync):
                    migration.op = Operations(MigrationContext.configure(sync))
                    migration.upgrade()
                await conn.run_sync(upgrade)
                rows = dict((await conn.execute(text("SELECT id, revision_id FROM indexing_job_documents"))).all())
                assert rows == {"legacy": "revision", "wrong-document": None, "plain": None}
                assert await conn.scalar(text("SELECT count(*) FROM pg_constraint WHERE conname='fk_indexing_job_documents_revision' AND connamespace=to_regnamespace(:schema)"), {"schema": schema}) == 1
                def downgrade(sync):
                    migration.op = Operations(MigrationContext.configure(sync))
                    migration.downgrade()
                await conn.run_sync(downgrade)
                assert await conn.scalar(text("SELECT count(*) FROM indexing_job_documents")) == 3
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
