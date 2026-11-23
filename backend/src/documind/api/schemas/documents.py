"""Request and response bodies for document endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from documind.domain.documents.files import DocumentFormat
from documind.domain.documents.status import DocumentStatus


class DocumentResponse(BaseModel):
    id: uuid.UUID
    title: str
    original_filename: str
    format: DocumentFormat
    media_type: str
    size_bytes: int
    checksum_sha256: str
    status: DocumentStatus
    processing_error: str | None
    page_count: int | None
    version_count: int
    current_version_id: uuid.UUID | None
    collection_ids: list[uuid.UUID]
    uploaded_by_user_id: uuid.UUID | None
    archived_at: datetime | None
    archived_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class DocumentPageResponse(BaseModel):
    items: list[DocumentResponse]
    total: int
    limit: int
    offset: int


class DocumentVersionResponse(BaseModel):
    id: uuid.UUID
    version_number: int
    original_filename: str
    format: DocumentFormat
    size_bytes: int
    checksum_sha256: str
    scan_status: str
    uploaded_by_user_id: uuid.UUID | None
    created_at: datetime
    is_current: bool


class DownloadUrlResponse(BaseModel):
    url: str
    expires_in: int
    filename: str


class RenameDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=500)


class CollectionResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    document_count: int
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class CreateCollectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)


class UpdateCollectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
