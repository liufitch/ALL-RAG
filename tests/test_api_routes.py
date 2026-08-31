import unittest
from datetime import datetime, timezone

from sqlalchemy import ARRAY

from main import app
from rag_modules.db.models import DatasetRecord, DocumentRecord, DocumentSegmentRecord
from rag_modules.repositories.knowledge_base_repository import KnowledgeBaseRepository
from rag_modules.services.knowledge_base_service import KnowledgeBaseService


class ApiRouteContractTest(unittest.TestCase):
    def test_knowledge_base_list_is_available_at_frontend_path(self) -> None:
        paths = app.openapi()["paths"]

        self.assertIn("/api/knowledge_base/list", paths)


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
    async def list(self, page: int, page_size: int):
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


class DatasetRepositoryContractTest(unittest.IsolatedAsyncioTestCase):
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
