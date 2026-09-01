from __future__ import annotations

from pydantic import BaseModel


class DocumentItem(BaseModel):
    """Public representation of an uploaded dataset document."""

    id: str
    dataset_id: str
    name: str
    status: str
    duplicate: bool = False


class DocumentRejection(BaseModel):
    """A single file that could not be accepted by the upload API."""

    filename: str
    code: str
    message: str


class DocumentUploadResponse(BaseModel):
    documents: list[DocumentItem]
    rejected: list[DocumentRejection]


class DocumentListResponse(BaseModel):
    items: list[DocumentItem]
    total: int


# Explicit aliases keep the DTO discoverable under the names used by callers
# while preserving one canonical schema in OpenAPI.
DocumentResponse = DocumentItem
RejectedDocument = DocumentRejection
