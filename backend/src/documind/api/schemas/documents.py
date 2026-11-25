"""Request and response bodies for document endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

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
    chunk_count: int | None
    processed_at: datetime | None
    version_count: int
    current_version_id: uuid.UUID | None
    collection_ids: list[uuid.UUID]
    uploaded_by_user_id: uuid.UUID | None
    archived_at: datetime | None
    archived_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    needs_reindex: bool = Field(
        default=False,
        description=(
            "Ready, but indexed with a different embedding model than the workspace uses now, "
            "so search by meaning can't find it until it is re-indexed (reprocessed)."
        ),
    )


class DocumentPageResponse(BaseModel):
    items: list[DocumentResponse]
    total: int
    limit: int
    offset: int
    needs_reindex_total: int = Field(
        default=0,
        description="Ready, non-archived documents in the workspace that need re-indexing.",
    )


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


class ProcessingJobResponse(BaseModel):
    id: uuid.UUID
    document_version_id: uuid.UUID
    status: str
    stage: str | None
    attempts: int
    max_attempts: int
    error_code: str | None
    last_error: str | None
    next_attempt_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    stats: dict[str, Any]
    created_at: datetime


class ProcessingStatusResponse(BaseModel):
    document_id: uuid.UUID
    status: DocumentStatus
    processing_error: str | None
    page_count: int | None
    chunk_count: int | None
    processed_at: datetime | None
    extraction_warnings: list[str]
    jobs: list[ProcessingJobResponse]


class ReprocessResponse(BaseModel):
    job_id: uuid.UUID


class PageResponse(BaseModel):
    position: int
    page_number: int | None
    text: str
    headings: list[str]
    ocr: bool


class PageListResponse(BaseModel):
    items: list[PageResponse]
    total: int


class ChunkResponse(BaseModel):
    chunk_index: int
    text: str
    token_count: int
    page_start: int | None
    page_end: int | None
    heading_path: list[str]
    embedding_model: str


class ChunkListResponse(BaseModel):
    items: list[ChunkResponse]
    total: int
