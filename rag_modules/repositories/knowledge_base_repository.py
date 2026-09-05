from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_modules.db.models import (
    DatasetIndexRecord, DatasetRecord, DocumentRecord, DocumentSegmentRecord,
    IndexingJobRecord,
)


def _summary(dataset_id: str | None = None):
    """列表、详情与统计共用同一汇总，保证状态和数量口径一致。"""
    documents = DocumentRecord
    usable_document = and_(documents.enabled.is_(True), documents.archived.is_(False))
    # 文档数量包含尚未索引的上传；禁用、归档文档不参与运行/失败状态判断。
    document_counts = select(
        documents.dataset_id,
        func.count(documents.id).label("document_count"),
        func.max(case((and_(usable_document, documents.indexing_status.in_(
            ("downloading", "parsing", "splitting", "embedding", "indexing", "queued", "retry_wait")
        )), 1), else_=0)).label("processing"),
        func.max(case((and_(usable_document, documents.indexing_status.in_(
            ("error", "failed")
        )), 1), else_=0)).label("failed"),
    ).where(documents.deleted_at.is_(None))
    if dataset_id is not None:
        document_counts = document_counts.where(documents.dataset_id == dataset_id)
    document_counts = document_counts.group_by(documents.dataset_id).subquery()

    segments = DocumentSegmentRecord
    indexes = DatasetIndexRecord
    segment_counts = (
        select(segments.dataset_id, func.count(segments.id).label("chunk_count"))
        .join(documents, and_(documents.id == segments.document_id,
                              documents.dataset_id == segments.dataset_id))
        .outerjoin(indexes, and_(indexes.id == segments.dataset_index_id,
                                indexes.dataset_id == segments.dataset_id))
        .where(
            segments.deleted_at.is_(None), segments.status == "completed",
            segments.embedding_status.in_(("completed", "not_required")),
            documents.deleted_at.is_(None), usable_document,
            # 新分段必须属于已激活索引；旧数据没有版本 ID，须以文档完成状态兜底。
            or_(
                and_(segments.dataset_index_id.is_(None),
                     documents.indexing_status == "completed"),
                and_(indexes.status == "active", indexes.deleted_at.is_(None)),
            ),
        )
    )
    if dataset_id is not None:
        segment_counts = segment_counts.where(segments.dataset_id == dataset_id)
    segment_counts = segment_counts.group_by(segments.dataset_id).subquery()

    jobs = IndexingJobRecord
    # 任何在途任务都代表正在处理；失败只看最近任务，避免历史失败永久污染状态。
    ranked_jobs = select(
        jobs.dataset_id, jobs.status,
        func.row_number().over(
            partition_by=jobs.dataset_id,
            order_by=(jobs.created_at.desc(), jobs.id.desc()),
        ).label("rank"),
    )
    if dataset_id is not None:
        ranked_jobs = ranked_jobs.where(jobs.dataset_id == dataset_id)
    ranked_jobs = ranked_jobs.subquery()
    job_counts = select(
        ranked_jobs.c.dataset_id,
        func.max(case((ranked_jobs.c.status.in_(
            ("pending", "queued", "running", "retry_wait")
        ), 1), else_=0)).label("processing"),
        func.max(case((and_(ranked_jobs.c.rank == 1, ranked_jobs.c.status.in_(
            ("failed", "partial_success")
        )), 1), else_=0)).label("failed"),
    ).group_by(ranked_jobs.c.dataset_id).subquery()

    chunk_count = func.coalesce(segment_counts.c.chunk_count, 0)
    # 优先级：处理中 > 存在可用分段 > 失败 > 草稿。
    # waiting 上传仍为草稿；重建时保留旧 active 分段计数，不把 staging 算作可用。
    status = case(
        (or_(document_counts.c.processing == 1, job_counts.c.processing == 1), "indexing"),
        (chunk_count > 0, "ready"),
        (or_(document_counts.c.failed == 1, job_counts.c.failed == 1), "failed"),
        else_="draft",
    )
    statement = (
        select(
            DatasetRecord.id.label("dataset_id"),
            func.coalesce(document_counts.c.document_count, 0).label("document_count"),
            chunk_count.label("chunk_count"), status.label("status"),
        )
        .outerjoin(document_counts, document_counts.c.dataset_id == DatasetRecord.id)
        .outerjoin(segment_counts, segment_counts.c.dataset_id == DatasetRecord.id)
        .outerjoin(job_counts, job_counts.c.dataset_id == DatasetRecord.id)
        .where(DatasetRecord.deleted_at.is_(None))
    )
    if dataset_id is not None:
        statement = statement.where(DatasetRecord.id == dataset_id)
    return statement.subquery()


class KnowledgeBaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(
        self, page: int, page_size: int, *, status: str = "all",
        visibility: str = "all", q: str | None = None,
    ) -> tuple[list[tuple[DatasetRecord, int, int, str]], int]:
        summary = _summary()
        filters = []
        if status != "all":
            filters.append(summary.c.status == status)
        permissions = {
            "private": ("private", "only_me"),
            "team": ("team", "all_team_members"),
            "public": ("public", "all_members"),
        }
        if visibility in permissions:
            filters.append(DatasetRecord.permission.in_(permissions[visibility]))
        if q:
            pattern = f"%{q}%"
            filters.append(or_(
                DatasetRecord.name.ilike(pattern),
                DatasetRecord.description.ilike(pattern),
                DatasetRecord.dataset_type.ilike(pattern),
            ))
        statement = (
            select(DatasetRecord, summary.c.document_count, summary.c.chunk_count, summary.c.status)
            .join(summary, summary.c.dataset_id == DatasetRecord.id)
            .where(*filters)
        )
        count_stmt = select(func.count()).select_from(statement.subquery())
        statement = statement.order_by(
            DatasetRecord.updated_at.desc().nullslast(), DatasetRecord.created_at.desc(),
            DatasetRecord.id.asc(),
        ).offset((page - 1) * page_size).limit(page_size)
        result = await self.session.execute(statement)
        total_result = await self.session.execute(count_stmt)
        return list(result.all()), int(total_result.scalar_one())

    async def stats(self) -> dict[str, int]:
        # 只返回一行聚合值，不构造 ORM/DTO 列表，也不受分页上限影响。
        summary = _summary()
        statement = select(
            func.count().label("total"),
            *(func.coalesce(func.sum(case((summary.c.status == status, 1), else_=0)), 0)
              .label(status) for status in ("ready", "indexing", "draft", "failed")),
            func.coalesce(func.sum(summary.c.document_count), 0).label("documents"),
            func.coalesce(func.sum(summary.c.chunk_count), 0).label("chunks"),
        ).select_from(summary)
        result = await self.session.execute(statement)
        return {key: int(value) for key, value in result.mappings().one().items()}

    async def get_active_with_counts(self, dataset_id: str) -> tuple[DatasetRecord, int, int, str] | None:
        summary = _summary(dataset_id)
        statement = select(
            DatasetRecord, summary.c.document_count, summary.c.chunk_count, summary.c.status,
        ).join(summary, summary.c.dataset_id == DatasetRecord.id)
        result = await self.session.execute(statement)
        return result.one_or_none()

    async def get_active(self, dataset_id: str) -> DatasetRecord | None:
        statement = select(DatasetRecord).where(
            DatasetRecord.id == dataset_id, DatasetRecord.deleted_at.is_(None),
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

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
