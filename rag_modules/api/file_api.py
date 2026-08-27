from fastapi import APIRouter, FastAPI

router = APIRouter(prefix="/api/file_manage", tags=["文件管理"])

@APIRouter.get("")
def file_manage():
    return {};