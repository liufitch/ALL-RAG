from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_modules.db.models import DocumentRecord


class DocumentRepository:
    """限定在单个知识库范围内的文档持久化操作。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_active_by_ids(
        self, dataset_id: str, document_ids: list[str]
    ) -> list[DocumentRecord]:
        """加载指定的有效文档，并确保它们全部属于同一个知识库。"""
        if not document_ids:
            return []
        stmt = select(DocumentRecord).where(
            DocumentRecord.dataset_id == dataset_id,
            DocumentRecord.id.in_(document_ids),
            DocumentRecord.deleted_at.is_(None),
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def next_position(self, dataset_id: str) -> int:
        """返回有效文档中的下一个位置编号，编号从 1 开始。"""
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
        """查找文件名和内容哈希均完全匹配的有效文档。

        哈希保存在 JSON 列中，其查询运算符在 SQLite 和 PostgreSQL 中不同。
        先按已建立索引的普通字段筛选，再用 Python 检查少量候选记录，以保持数据库兼容性。
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
        """列出属于指定知识库的有效文档。

        结果查询和计数查询均包含知识库筛选，避免调用方在按名称或状态筛选时，
        意外暴露其他知识库的文档。
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
