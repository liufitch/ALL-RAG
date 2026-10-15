from rag_modules.graph.extraction import Entity, GraphExtraction, Relation
from rag_modules.graph.neo4j_store import Neo4jGraphStore


class FakeTx:
    def __init__(self):
        self.calls = []

    def run(self, statement, **params):
        self.calls.append((statement, params))


class FakeSession:
    def __init__(self):
        self.tx = FakeTx()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute_write(self, callback):
        return callback(self.tx)


class FakeDriver:
    def __init__(self):
        self.current = None
        self.search_calls = []

    def session(self, *, database):
        self.current = FakeSession()
        return self.current

    def execute_query(self, statement, **params):
        self.search_calls.append((statement, params))
        return ([{"id": "seg-1", "score": 1.0}], None, None)


def test_upsert_scopes_entities_to_index_and_replaces_segment_facts():
    driver = FakeDriver()
    store = Neo4jGraphStore(driver, database="neo4j")
    extraction = GraphExtraction(
        entities=(Entity("腾讯", "organization", {}), Entity("深圳", "location", {})),
        relations=(Relation("腾讯", "深圳", "位于"),),
    )

    store.upsert_segment("dataset-1", "index-1", "doc-1", "seg-1", extraction)
    calls = driver.current.tx.calls
    assert len(calls) >= 3
    assert calls[-2][1]["segment_id"] == "seg-1"
    assert all(item["index_id"] == "index-1" for item in calls[-3][1]["entities"])
    assert calls[-2][1]["relations"][0]["type"] == "位于"
    assert any("DELETE" in statement for statement, _ in calls[:-1])


def test_search_is_scoped_to_active_index_and_returns_segment_ids():
    driver = FakeDriver()
    store = Neo4jGraphStore(driver, database="neo4j")
    hits = store.search("index-1", "腾讯在哪", 5)
    assert [(hit.id, hit.score) for hit in hits] == [("seg-1", 1.0)]
    statement, params = driver.search_calls[0]
    assert params["index_id"] == "index-1"
    assert params["query"] == "腾讯在哪"
    assert "SourceSegment" in statement


def test_replacing_a_segment_removes_orphaned_entities():
    driver = FakeDriver()
    store = Neo4jGraphStore(driver, database="neo4j")
    store.upsert_segment("d", "i", "doc", "segment", GraphExtraction((), ()))
    assert any("NOT (e)<-[:MENTIONS]-()" in statement for statement, _ in driver.current.tx.calls)
