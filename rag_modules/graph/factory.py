"""Connect for enabled graph operations or explicit historical asset cleanup."""

from functools import lru_cache

from rag_modules.config.settings import settings

from .neo4j_store import Neo4jGraphStore


@lru_cache(maxsize=2)
def get_graph_store(*, for_cleanup: bool = False) -> Neo4jGraphStore | None:
    if not settings.graph.enabled and not for_cleanup:
        return None
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.graph.uri,
        auth=(settings.graph.username, settings.graph.password.get_secret_value()),
    )
    store = Neo4jGraphStore(driver, database=settings.graph.database)
    try:
        if not for_cleanup:
            store.ensure_schema()
    except Exception:
        driver.close()
        raise
    return store
