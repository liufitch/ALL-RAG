from fastapi import Depends, HTTPException
from sqlalchemy import select

from rag_modules.db.models import DatasetRecord
from rag_modules.db.session import get_db_session


async def require_active_dataset(dataset_id: str, db=Depends(get_db_session)):
    dataset = await db.scalar(select(DatasetRecord).where(
        DatasetRecord.id == dataset_id, DatasetRecord.deleted_at.is_(None),
    ))
    if dataset is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return dataset
