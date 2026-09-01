import unittest
from datetime import datetime, timezone

from sqlalchemy import ARRAY
from sqlalchemy.dialects import postgresql

from main import app
from rag_modules.db.models import DatasetRecord, DocumentRecord, DocumentSegmentRecord
from rag_modules.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag_modules.services.knowledge_base_service import KnowledgeBaseService


class ApiRouteContractTest(unittest.TestCase):
    def test_knowledge_base_list_is_available_at_frontend_path(self) -> None:
        paths = app.openapi()["paths"]

        self.assertIn("/api/knowledge_base/list", paths)

    def test_dataset_create_and_detail_contracts_are_in_openapi(self) -> None:
        schema = app.openapi()

        self.assertIn("201", schema["paths"]["/api/knowledge_base"]["post"]["responses"])
        self.assertIn("get", schema["paths"]["/api/knowledge_base/{dataset_id}"])
        create_schema = schema["components"]["schemas"]["KnowledgeBaseCreate"]
        self.assertEqual(
            set(create_schema["properties"]),
            {"name", "description", "permission"},
        )
        self.assertFalse(create_schema["additionalProperties"])


class DatasetSchemaContractTest(unittest.TestCase):
    def test_orm_models_match_existing_dataset_tables(self) -> None:
        self.assertEqual(DatasetRecord.__tablename__, "datasets")
        self.assertEqual(DocumentRecord.__tablename__, "documents")
        self.assertEqual(DocumentSegmentRecord.__tablename__, "document_segments")

        self.assertIn("created_by", DatasetRecord.__table__.c)
        self.assertIn("deleted_at", DatasetRecord.__table__.c)
        self.assertIn("dataset_id", DocumentRecord.__table__.c)
        self.assertIn("dataset_id", DocumentSegmentRecord.__table__.c)
        self.assertIn("embedding_status", DocumentSegmentRecord.__table__.c)
        self.assertIsInstance(DocumentSegmentRecord.__table__.c.keywords.type, ARRAY)


class _DatasetRepositoryStub:
    async def list(
        self,
        page: int,
        page_size: int,
        *,
        status: str = "all",
        visibility: str = "all",
        q: str | None = None,
    ):
        now = datetime.now(timezone.utc)
        dataset = DatasetRecord(
            id="dataset-1",
            name="Dataset One",
            description=None,
            provider="vendor",
            permission="all_team_members",
            dataset_type=None,
            indexing_technique="high_quality",
            created_by="user-1",
            created_at=now,
            updated_at=None,
            embedding_model=None,
            retrieval_model_config=None,
            partial_user_config=None,
        )
        return [(dataset, 2, 7)], 1


class DatasetServiceContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_list_maps_dataset_and_related_counts_to_api_dto(self) -> None:
        service = KnowledgeBaseService(_DatasetRepositoryStub())

        items, total = await service.list_knowledge_bases(page=1, page_size=10)

        self.assertEqual(total, 1)
        self.assertEqual(items[0].visibility, "team")
        self.assertEqual(items[0].document_count, 2)
        self.assertEqual(items[0].chunk_count, 7)
        self.assertEqual(items[0].updated_at, items[0].created_at)

    async def test_stats_aggregate_dataset_and_related_counts(self) -> None:
        service = KnowledgeBaseService(_DatasetRepositoryStub())

        stats = await service.knowledge_base_stats()

        self.assertEqual(
            stats,
            {
                "total": 1,
                "ready": 1,
                "indexing": 0,
                "draft": 0,
                "documents": 2,
                "chunks": 7,
            },
        )


class _DeleteSessionStub:
    def __init__(self, dataset: DatasetRecord) -> None:
        self.dataset = dataset
        self.committed = False

    async def get(self, model, record_id: str):
        if model is DatasetRecord and record_id == self.dataset.id:
            return self.dataset
        return None

    async def commit(self) -> None:
        self.committed = True


class _ScalarResultStub:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def one_or_none(self):
        return self.value


class _SelectSessionStub:
    def __init__(self, value) -> None:
        self.value = value
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _ScalarResultStub(self.value)


class DatasetRepositoryContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_get_active_returns_only_active_dataset_by_id(self) -> None:
        now = datetime.now(timezone.utc)
        dataset = DatasetRecord(
            id="dataset-1",
            name="Dataset One",
            provider="vendor",
            permission="only_me",
            indexing_technique="high_quality",
            created_by="user-1",
            created_at=now,
        )
        session = _SelectSessionStub(dataset)
        repository = KnowledgeBaseRepository(session)

        found = await repository.get_active("dataset-1")

        self.assertIs(found, dataset)
        sql = str(
            session.statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.assertIn("datasets.id = 'dataset-1'", sql)
        self.assertIn("datasets.deleted_at IS NULL", sql)

    async def test_get_active_with_counts_uses_active_correlated_counts(self) -> None:
        now = datetime.now(timezone.utc)
        dataset = DatasetRecord(
            id="dataset-1",
            name="Dataset One",
            provider="vendor",
            permission="only_me",
            indexing_technique="high_quality",
            created_by="user-1",
            created_at=now,
        )
        session = _SelectSessionStub((dataset, 2, 7))
        repository = KnowledgeBaseRepository(session)

        found = await repository.get_active_with_counts("dataset-1")

        self.assertEqual(found, (dataset, 2, 7))
        sql = str(
            session.statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.assertIn("datasets.id = 'dataset-1'", sql)
        self.assertIn("datasets.deleted_at IS NULL", sql)
        self.assertIn("count(documents.id)", sql)
        self.assertIn("documents.dataset_id = datasets.id", sql)
        self.assertIn("documents.deleted_at IS NULL", sql)
        self.assertIn("count(document_segments.id)", sql)
        self.assertIn("document_segments.dataset_id = datasets.id", sql)
        self.assertIn("document_segments.deleted_at IS NULL", sql)

    async def test_delete_marks_dataset_deleted_without_physical_delete(self) -> None:
        now = datetime.now(timezone.utc)
        dataset = DatasetRecord(
            id="dataset-1",
            name="Dataset One",
            provider="vendor",
            permission="private",
            indexing_technique="high_quality",
            created_by="user-1",
            created_at=now,
        )
        session = _DeleteSessionStub(dataset)
        repository = KnowledgeBaseRepository(session)

        deleted = await repository.soft_delete("dataset-1")

        self.assertTrue(deleted)
        self.assertIsNotNone(dataset.deleted_at)
        self.assertTrue(session.committed)


if __name__ == "__main__":
    unittest.main()
