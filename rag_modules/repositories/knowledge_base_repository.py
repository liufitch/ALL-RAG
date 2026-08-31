from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_modules.db.models import DatasetRecord, DocumentRecord, DocumentSegmentRecord


class KnowledgeBaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(
        self, page: int, page_size: int
    ) -> tuple[list[tuple[DatasetRecord, int, int]], int]:
        document_count = (
            select(func.count(DocumentRecord.id))
            .where(
                DocumentRecord.dataset_id == DatasetRecord.id,
                DocumentRecord.deleted_at.is_(None),
            )
            .correlate(DatasetRecord)
            .scalar_subquery()
        )
        segment_count = (
            select(func.count(DocumentSegmentRecord.id))
            .where(
                DocumentSegmentRecord.dataset_id == DatasetRecord.id,
                DocumentSegmentRecord.deleted_at.is_(None),
            )
            .correlate(DatasetRecord)
            .scalar_subquery()
        )
        stmt = (
            select(DatasetRecord, document_count, segment_count)
            .where(DatasetRecord.deleted_at.is_(None))
            .order_by(DatasetRecord.updated_at.desc().nullslast(), DatasetRecord.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        count_stmt = (
            select(func.count())
            .select_from(DatasetRecord)
            .where(DatasetRecord.deleted_at.is_(None))
        )
        result = await self.session.execute(stmt)
        total_result = await self.session.execute(count_stmt)
        return list(result.all()), int(total_result.scalar_one())

    async def create(self, record: DatasetRecord) -> DatasetRecord:
        self.session.add(record)
        await self.session.commit()
        await self.session.refresh(record)
        return record

    async def soft_delete(self, dataset_id: str) -> bool:
        record = await self.session.get(DatasetRecord, dataset_id)
        if record is None or record.deleted_at is not None:
            return False
        record.deleted_at = datetime.now(timezone.utc)
        await self.session.commit()
        return True
