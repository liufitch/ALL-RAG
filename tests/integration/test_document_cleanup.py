"""Opt-in checks using only isolated, test-owned remote assets."""
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from rag_modules.config.settings import settings
from rag_modules.graph.extraction import Entity, GraphExtraction, Relation
from rag_modules.graph.neo4j_store import Neo4jGraphStore
from rag_modules.vector_stores.base import VectorEntity

pytestmark = pytest.mark.integration


def require_integration():
    if os.getenv("RUN_INTEGRATION") != "1":
        pytest.skip("set RUN_INTEGRATION=1 to test live cleanup backends")


def test_neo4j_deletes_document_facts_across_indexes_and_preserves_shared_entities():
    require_integration()
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(os.getenv("TEST_NEO4J_URI", "bolt://localhost:7687"), auth=(settings.graph.username, os.getenv("TEST_NEO4J_PASSWORD", settings.graph.password.get_secret_value())))
    driver.verify_connectivity()
    store = Neo4jGraphStore(driver, database=settings.graph.database)
    dataset, other_dataset = uuid4().hex, uuid4().hex
    indexes = [uuid4().hex for _ in range(3)]
    first, second = uuid4().hex, uuid4().hex
    first_segment, second_segment, old_segment, unrelated = [uuid4().hex for _ in range(4)]
    common = (Entity("Shared", "concept", {}), Entity("Keeper", "concept", {}))
    extraction = GraphExtraction(common + (Entity("Exclusive", "concept", {}),), (Relation("Shared", "Keeper", "old-fact"), Relation("Shared", "Exclusive", "only-first")))
    try:
        store.upsert_segment(dataset, indexes[0], first, first_segment, extraction)
        store.upsert_segment(dataset, indexes[0], second, second_segment, GraphExtraction(common, (Relation("Shared", "Keeper", "retained-fact"),)))
        store.upsert_segment(dataset, indexes[1], first, old_segment, extraction)
        store.upsert_segment(other_dataset, indexes[2], first, unrelated, extraction)
        store.delete_document(dataset, first)
        store.delete_document(dataset, first)
        records, _, _ = driver.execute_query("MATCH (s:SourceSegment) WHERE s.dataset_index_id IN $indexes RETURN s.id AS id", indexes=indexes, database_=store.database)
        assert {r["id"] for r in records} == {second_segment, unrelated}
        records, _, _ = driver.execute_query("MATCH (e:Entity {dataset_index_id: $index}) RETURN e.name AS name", index=indexes[0], database_=store.database)
        assert {r["name"] for r in records} == {"Shared", "Keeper"}
        records, _, _ = driver.execute_query("MATCH ()-[r:RELATED]->() WHERE r.segment_id IN $segments RETURN r.segment_id AS id, r.type AS type", segments=[first_segment, second_segment, old_segment, unrelated], database_=store.database)
        assert {(r["id"], r["type"]) for r in records} == {(second_segment, "retained-fact"), (unrelated, "old-fact"), (unrelated, "only-first")}
    finally:
        driver.execute_query("MATCH (n) WHERE n.dataset_index_id IN $indexes DETACH DELETE n", indexes=indexes, database_=store.database)
        driver.close()


def test_milvus_document_deletion_preserves_other_documents_and_missing_collection(real_milvus_store):
    collection = "cleanup_" + uuid4().hex
    dataset, index, first, second = [uuid4().hex for _ in range(4)]
    try:
        real_milvus_store.ensure_collection(collection, 3)
        real_milvus_store.upsert(collection, [VectorEntity(id=uuid4().hex, embedding=(.1, .2, .3), dataset_id=dataset, dataset_index_id=index, document_id=doc, position=0) for doc in [first, second]])
        assert real_milvus_store.count(collection) == 2
        real_milvus_store.delete_document(collection, first)
        real_milvus_store.delete_document(collection, first)
        assert real_milvus_store.count(collection) == 1
        hits = real_milvus_store._client().query(collection_name=collection, filter="", output_fields=["document_id"], limit=10)
        assert [hit["document_id"] for hit in hits] == [second]
    finally:
        real_milvus_store.drop_collection(collection)
    assert real_milvus_store.delete_document(collection, first) == 0


@pytest.mark.asyncio
async def test_cleanup_migration_on_isolated_postgresql_schema():
    require_integration()
    if settings.database.type != "postgresql":
        pytest.skip("requires PostgreSQL configuration")
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[2] / "migrations/versions/20261116_01_document_cleanup.py"
    spec = importlib.util.spec_from_file_location("cleanup_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = "cleanup_test_" + uuid4().hex
    engine = create_async_engine(settings.sqlalchemy_database_uri, hide_parameters=True)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                await conn.execute(text("CREATE TABLE documents (id text PRIMARY KEY, deleted_at timestamptz)"))
                await conn.execute(text("INSERT INTO documents (id, deleted_at) VALUES ('legacy-deleted', now())"))
                def upgrade(sync_conn):
                    migration.op = Operations(MigrationContext.configure(sync_conn))
                    migration.upgrade()
                await conn.run_sync(upgrade)
                row = (await conn.execute(text("SELECT cleanup_completed_at, cleanup_last_attempt_at FROM documents WHERE id = 'legacy-deleted'"))).one()
                assert tuple(row) == (None, None)
                assert await conn.scalar(text("SELECT count(*) FROM pg_indexes WHERE schemaname = :schema AND indexname = 'ix_documents_pending_cleanup'"), {"schema": schema}) == 1
                def downgrade(sync_conn):
                    migration.op = Operations(MigrationContext.configure(sync_conn))
                    migration.downgrade()
                await conn.run_sync(downgrade)
                assert await conn.scalar(text("SELECT count(*) FROM documents")) == 1
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
