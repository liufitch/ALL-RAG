from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class DocumentItem(BaseModel):
    """已上传知识库文档的对外数据表示。"""

    id: str
    dataset_id: str
    name: str
    status: str
    duplicate: bool = False
    enabled: bool = True
    archived: bool = False
    updated_at: datetime | None = None
    error: str | None = None
    size: int | None = None
    content_type: str | None = None
    segment_count: int | None = None


class DocumentRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Document name must not be blank")
        return value


class DocumentActionResponse(BaseModel):
    document: DocumentItem
    job_id: str | None = None


class DocumentSegmentItem(BaseModel):
    id: str
    position: int
    content: str
    question: str | None = None
    answer: str | None = None
    keywords: list[str] = Field(default_factory=list)
    parent_id: str | None = None
    index_type: str
    source_metadata: dict = Field(default_factory=dict)


class DocumentSegmentListResponse(BaseModel):
    document_id: str
    revision: int | None = None
    items: list[DocumentSegmentItem]


class DocumentSegmentUpdate(BaseModel):
    base_version: int | None = Field(default=None, ge=0)
    segments: list[DocumentSegmentItem] = Field(min_length=1, max_length=10000)


class DocumentRevisionResponse(BaseModel):
    revision_id: str
    version: int
    job_id: str


class DocumentJobResponse(BaseModel):
    id: str
    status: str
    progress: int
    current_stage: str | None = None
    error: str | None = None


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
