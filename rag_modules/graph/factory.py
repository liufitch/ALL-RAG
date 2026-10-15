"""只在启用时建立 Neo4j 连接。"""

from functools import lru_cache

from rag_modules.config.settings import settings

from .neo4j_store import Neo4jGraphStore


@lru_cache(maxsize=1)
def get_graph_store() -> Neo4jGraphStore | None:
    if not settings.graph.enabled:
        return None
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.graph.uri,
        auth=(settings.graph.username, settings.graph.password.get_secret_value()),
    )
    store = Neo4jGraphStore(driver, database=settings.graph.database)
    try:
        store.ensure_schema()
    except Exception:
        driver.close()
        raise
    return store
