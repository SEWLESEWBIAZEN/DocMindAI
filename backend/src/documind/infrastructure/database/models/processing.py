"""Processing pipeline persistence: jobs (outbox + state machine), pages, chunks."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from documind.domain.processing.vectors import VECTOR_DIMENSIONS
from documind.infrastructure.database.base import Base

TZ = DateTime(timezone=True)
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "dead", "superseded", "cancelled")
ACTIVE_JOB_STATUSES = ("queued", "running")


def _document_fk(name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["document_id", "workspace_id"],
        ["documents.id", "documents.workspace_id"],
        ondelete="CASCADE",
        name=name,
    )


class ProcessingJob(Base):
    """One attempt-tracked run of the pipeline for a document version.

    Created in the same transaction as the upload (transactional outbox), so a
    committed document always has a durable job even if queue publishing fails.
    """

    __tablename__ = "processing_jobs"
    __table_args__ = (
        _document_fk("fk_processing_jobs_document_workspace"),
        CheckConstraint(f"status IN {JOB_STATUSES}", name="status_valid"),
        # At most one active job per document.
        Index(
            "uq_processing_jobs_active_document",
            "document_id",
            unique=True,
            postgresql_where=sql_text("status IN ('queued', 'running')"),
        ),
        Index("ix_processing_jobs_status_next_attempt_at", "status", "next_attempt_at"),
        Index("ix_processing_jobs_document_id_created_at", "document_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    document_id: Mapped[uuid.UUID] = mapped_column()
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued")
    stage: Mapped[str | None] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    dispatched_at: Mapped[datetime | None] = mapped_column(TZ)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TZ)
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)
    worker: Mapped[str | None] = mapped_column(String(128))
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class DocumentPage(Base):
    """Extracted, normalised text of one page (or the whole text for unpaginated formats)."""

    __tablename__ = "document_pages"
    __table_args__ = (
        _document_fk("fk_document_pages_document_workspace"),
        UniqueConstraint("document_version_id", "position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    document_id: Mapped[uuid.UUID] = mapped_column()
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    page_number: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    headings: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    ocr: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sql_text("false"))
    char_count: Mapped[int] = mapped_column(Integer)


class DocumentChunk(Base):
    """A retrievable passage with its embedding and citation metadata."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        _document_fk("fk_document_chunks_document_workspace"),
        UniqueConstraint("document_version_id", "chunk_index"),
        Index("ix_document_chunks_workspace_id_document_id", "workspace_id", "document_id"),
        # Embedding re-use lookups.
        Index(
            "ix_document_chunks_workspace_id_content_sha256",
            "workspace_id",
            "content_sha256",
            "embedding_model",
        ),
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_document_chunks_search_tsv", "search_tsv", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    document_id: Mapped[uuid.UUID] = mapped_column()
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    heading_path: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    content_sha256: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list[float]] = mapped_column(Vector(VECTOR_DIMENSIONS))
    embedding_model: Mapped[str] = mapped_column(String(100))
    embedding_dimensions: Mapped[int] = mapped_column(Integer)
    # Keyword side of hybrid search; maintained by Postgres (migration 0006).
    search_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english'::regconfig, text)", persisted=True)
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
