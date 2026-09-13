from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentItem(BaseModel):
    """已上传知识库文档的对外数据表示。"""

    id: str
    dataset_id: str
    name: str
    status: str
    duplicate: bool = False


class DocumentRejection(BaseModel):
    """上传 API 无法接受的单个文件。"""

    filename: str
    code: str
    message: str


class DocumentUploadResponse(BaseModel):
    """批量上传结果；索引任务采用异步投递，不等待 Worker 完成。"""

    documents: list[DocumentItem]
    rejected: list[DocumentRejection]
    indexing_task_ids: list[str] = Field(default_factory=list)
    indexing_dispatch_pending: list[str] = Field(default_factory=list)


class DocumentListResponse(BaseModel):
    items: list[DocumentItem]
    total: int


# 显式别名让调用方能按约定名称找到 DTO，
# 同时在 OpenAPI 中只保留一份规范的数据结构定义。
DocumentResponse = DocumentItem
RejectedDocument = DocumentRejection
