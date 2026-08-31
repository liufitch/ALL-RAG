from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile

router = APIRouter(prefix="/api/file_manage", tags=["文件管理"])


UPLOAD_DIR = Path(__file__).resolve().parents[2] / "data" / "uploads"


@router.get("")
def file_manage() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)) -> dict[str, str]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "").suffix
    target = UPLOAD_DIR / f"{uuid4().hex}{suffix}"
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")
    target.write_bytes(content)
    return {
        "filename": file.filename or "",
        "stored_name": target.name,
        "path": str(target),
    }