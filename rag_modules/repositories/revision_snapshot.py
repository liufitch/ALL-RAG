"""Bind immutable input versions when a job is created, never when it retries."""
from sqlalchemy import select

from rag_modules.db.models import DocumentRevisionRecord


async def bind_document_revisions(session, dataset_id, rows):
    if not rows:
        return
    revisions = await session.scalars(select(DocumentRevisionRecord).where(
        DocumentRevisionRecord.dataset_id == dataset_id,
        DocumentRevisionRecord.document_id.in_([row.document_id for row in rows]),
    ).order_by(DocumentRevisionRecord.version.desc()))
    latest = {}
    for revision in revisions:
        latest.setdefault(revision.document_id, revision.id)
    for row in rows:
        row.revision_id = latest.get(row.document_id)
