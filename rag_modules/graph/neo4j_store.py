"""基于来源分段的 Neo4j 实体关系图。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from .extraction import GraphExtraction


@dataclass(frozen=True, slots=True)
class GraphHit:
    id: str
    score: float


class Neo4jGraphStore:
    def __init__(self, driver, *, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def close(self) -> None:
        self.driver.close()

    def ensure_schema(self) -> None:
        self.driver.execute_query(
            "CREATE CONSTRAINT graph_entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
            database_=self.database,
        )
        self.driver.execute_query(
            "CREATE CONSTRAINT graph_segment_id IF NOT EXISTS FOR (s:SourceSegment) REQUIRE s.id IS UNIQUE",
            database_=self.database,
        )
        self.driver.execute_query(
            "CREATE INDEX graph_entity_index IF NOT EXISTS FOR (e:Entity) ON (e.dataset_index_id)",
            database_=self.database,
        )

    def upsert_segment(
        self, dataset_id: str, index_id: str, document_id: str,
        segment_id: str, extraction: GraphExtraction,
    ) -> None:
        def entity_id(name: str) -> str:
            return sha256(f"{index_id}\0{name.casefold()}".encode()).hexdigest()

        entities = [
            {"id": entity_id(entity.name), "name": entity.name, "type": entity.type,
             "attributes": [f"{key}: {value}" for key, value in entity.attributes.items()],
             "index_id": index_id}
            for entity in extraction.entities
        ]
        relations = [
            {"source_id": entity_id(relation.source), "target_id": entity_id(relation.target),
             "type": relation.type}
            for relation in extraction.relations
        ]

        def write(tx):
            tx.run("MATCH (s:SourceSegment {id: $segment_id}) DETACH DELETE s", segment_id=segment_id)
            tx.run("MATCH ()-[r:RELATED {segment_id: $segment_id}]->() DELETE r", segment_id=segment_id)
            tx.run(
                "MERGE (s:SourceSegment {id: $segment_id}) "
                "SET s.dataset_id = $dataset_id, s.dataset_index_id = $index_id, s.document_id = $document_id "
                "WITH s UNWIND $entities AS item "
                "MERGE (e:Entity {id: item.id}) "
                "SET e.name = item.name, e.type = item.type, e.attributes = item.attributes, "
                "e.dataset_index_id = item.index_id "
                "MERGE (s)-[:MENTIONS]->(e)",
                segment_id=segment_id, dataset_id=dataset_id, index_id=index_id,
                document_id=document_id, entities=entities, relations=relations,
            )
            tx.run(
                "UNWIND $relations AS item "
                "MATCH (a:Entity {id: item.source_id}), (b:Entity {id: item.target_id}) "
                "MERGE (a)-[r:RELATED {segment_id: $segment_id, type: item.type}]->(b)",
                segment_id=segment_id, relations=relations,
            )
            tx.run(
                "MATCH (e:Entity {dataset_index_id: $index_id}) "
                "WHERE NOT (e)<-[:MENTIONS]-() DETACH DELETE e",
                index_id=index_id,
            )

        with self.driver.session(database=self.database) as session:
            session.execute_write(write)

    def search(self, index_id: str, query: str, limit: int) -> list[GraphHit]:
        records, _, _ = self.driver.execute_query(
            "MATCH (e:Entity {dataset_index_id: $index_id}) "
            "WHERE size(e.name) >= 2 AND toLower($query) CONTAINS toLower(e.name) "
            "MATCH (e)-[:RELATED*0..1]-(related:Entity)<-[:MENTIONS]-(s:SourceSegment {dataset_index_id: $index_id}) "
            "RETURN DISTINCT s.id AS id, 1.0 AS score LIMIT $limit",
            index_id=index_id, query=query, limit=limit, database_=self.database,
        )
        return [GraphHit(str(record["id"]), float(record["score"])) for record in records]
