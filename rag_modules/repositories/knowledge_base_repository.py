from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_modules.db.models import DatasetRecord, DocumentRecord, DocumentSegmentRecord


class KnowledgeBaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(
        self,
        page: int,
        page_size: int,
        *,
        status: str = "all",
        visibility: str = "all",
        q: str | None = None,
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
        active_document_exists = (
            select(DocumentRecord.id)
            .where(
                DocumentRecord.dataset_id == DatasetRecord.id,
                DocumentRecord.deleted_at.is_(None),
            )
            .correlate(DatasetRecord)
            .exists()
        )
        filters = [DatasetRecord.deleted_at.is_(None)]
        if status == "draft":
            filters.append(~active_document_exists)
        elif status == "ready":
            filters.append(active_document_exists)
        elif status in {"indexing", "failed"}:
            filters.append(false())

        permissions = {
            "private": ("private", "only_me"),
            "team": ("team", "all_team_members"),
            "public": ("public", "all_members"),
        }
        if visibility in permissions:
            filters.append(DatasetRecord.permission.in_(permissions[visibility]))

        if q:
            pattern = f"%{q}%"
            filters.append(
                or_(
                    DatasetRecord.name.ilike(pattern),
                    DatasetRecord.description.ilike(pattern),
                    DatasetRecord.dataset_type.ilike(pattern),
                )
            )

        stmt = (
            select(DatasetRecord, document_count, segment_count)
            .where(*filters)
            .order_by(DatasetRecord.updated_at.desc().nullslast(), DatasetRecord.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        count_stmt = (
            select(func.count())
            .select_from(DatasetRecord)
            .where(*filters)
        )
        result = await self.session.execute(stmt)
        total_result = await self.session.execute(count_stmt)
        return list(result.all()), int(total_result.scalar_one())

    async def get_active_with_counts(
        self,
        dataset_id: str,
    ) -> tuple[DatasetRecord, int, int] | None:
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
        stmt = select(DatasetRecord, document_count, segment_count).where(
            DatasetRecord.id == dataset_id,
            DatasetRecord.deleted_at.is_(None),
        )
        result = await self.session.execute(stmt)
        return result.one_or_none()

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
