from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_modules.db.models import DocumentRecord


class DocumentRepository:
    """Persistence operations for dataset-scoped documents."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def next_position(self, dataset_id: str) -> int:
        """Return the next one-based position among active documents."""
        stmt = select(func.max(DocumentRecord.position)).where(
            DocumentRecord.dataset_id == dataset_id,
            DocumentRecord.deleted_at.is_(None),
        )
        result = await self.session.execute(stmt)
        maximum = result.scalar_one_or_none()
        return (maximum or 0) + 1

    async def find_duplicate(
        self, dataset_id: str, sha256: str, filename: str
    ) -> DocumentRecord | None:
        """Find an active document with the exact filename and content hash.

        Hashes live in a JSON column whose operators differ between SQLite and
        PostgreSQL.  Filtering by the indexed scalar fields first and checking
        the small candidate set in Python keeps this method portable.
        """
        stmt = (
            select(DocumentRecord)
            .where(
                DocumentRecord.dataset_id == dataset_id,
                DocumentRecord.name == filename,
                DocumentRecord.deleted_at.is_(None),
            )
            .order_by(DocumentRecord.position.asc(), DocumentRecord.id.asc())
        )
        result = await self.session.execute(stmt)
        for record in result.scalars():
            info = record.data_source_info or {}
            if info.get("sha256") == sha256:
                return record
        return None

    async def create(self, record: DocumentRecord) -> DocumentRecord:
        self.session.add(record)
        try:
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise
        return record

    async def list(
        self,
        dataset_id: str,
        page: int,
        page_size: int,
        *,
        status: str | None = None,
        q: str | None = None,
    ) -> tuple[list[DocumentRecord], int]:
        """List active documents belonging to one dataset.

        Dataset filtering is part of both the result and count queries so a
        caller cannot accidentally expose another dataset's documents when a
        name or status filter is applied.
        """
        filters = [
            DocumentRecord.dataset_id == dataset_id,
            DocumentRecord.deleted_at.is_(None),
        ]
        if status:
            filters.append(DocumentRecord.indexing_status == status)
        if q:
            filters.append(DocumentRecord.name.ilike(f"%{q}%"))

        stmt = (
            select(DocumentRecord)
            .where(*filters)
            .order_by(DocumentRecord.position.asc(), DocumentRecord.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        count_stmt = select(func.count()).select_from(DocumentRecord).where(*filters)
        result = await self.session.execute(stmt)
        total_result = await self.session.execute(count_stmt)
        return list(result.scalars()), int(total_result.scalar_one())
