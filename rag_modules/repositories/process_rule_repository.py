from __future__ import annotations

from sqlalchemy import func, select

from rag_modules.db.models import (
    DatasetIndexRecord,
    DocumentRecord,
    IndexingJobDocumentRecord,
    IndexingJobRecord,
    ProcessRuleRecord,
)


class ProcessRuleRepository:
    def __init__(self, session):
        self.session = session

    async def get_active_documents(self, dataset_id: str, document_ids: list[str]):
        result = await self.session.execute(
            select(DocumentRecord).where(
                DocumentRecord.dataset_id == dataset_id,
                DocumentRecord.id.in_(document_ids),
                DocumentRecord.deleted_at.is_(None),
            ).order_by(DocumentRecord.position.asc(), DocumentRecord.id.asc())
        )
        return list(result.scalars())

    async def find_rule(self, dataset_id: str, config_hash: str):
        result = await self.session.execute(
            select(ProcessRuleRecord).where(
                ProcessRuleRecord.dataset_id == dataset_id,
                ProcessRuleRecord.config_hash == config_hash,
                ProcessRuleRecord.enabled.is_(True),
                ProcessRuleRecord.deleted_at.is_(None),
            ).order_by(ProcessRuleRecord.version.desc())
        )
        return result.scalars().first()

    async def create_rule_and_job(self, *, rule, job, index, documents, job_documents, rule_is_new=True):
        if rule_is_new:
            self.session.add(rule)
        self.session.add(index)
        self.session.add(job)
        self.session.add_all(job_documents)
        for document in documents:
            document.dataset_process_rule_id = rule.id
            document.indexing_status = "queued"
            document.updated_at = rule.created_at
        await self.session.commit()
        await self.session.refresh(rule)
        await self.session.refresh(job)
        return rule, job, job_documents
