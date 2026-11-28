"""Document management persistence models.

Tenant integrity is enforced by the database as well as the application:
child rows reference parents through composite ``(id, workspace_id)`` foreign
keys, so a version or collection item can never point at a document or
collection in a different workspace.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.infrastructure.database.base import Base

TZ = DateTime(timezone=True)
_STATUSES = "'pending', 'processing', 'ready', 'failed'"
_FORMATS = "'pdf', 'docx', 'txt', 'markdown'"


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_documents_id_workspace_id"),
        CheckConstraint(f"status IN ({_STATUSES})", name="status_valid"),
        CheckConstraint(f"format IN ({_FORMATS})", name="format_valid"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        # The same content can't be uploaded twice into one workspace.
        Index(
            "uq_documents_workspace_id_checksum_sha256",
            "workspace_id",
            "checksum_sha256",
            unique=True,
        ),
        Index("ix_documents_workspace_id_created_at", "workspace_id", "created_at"),
        Index("ix_documents_workspace_id_status", "workspace_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255))
    # Describes the current version (denormalised for listing and search).
    original_filename: Mapped[str] = mapped_column(String(255))
    format: Mapped[str] = mapped_column(String(16))
    media_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="SET NULL", use_alter=True)
    )
    version_count: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    processing_error: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Archived documents are read-only and hidden by default (and, from
    # Phase 5, excluded from retrieval). Restorable; deletion is separate.
    archived_at: Mapped[datetime | None] = mapped_column(TZ)
    archived_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Results of the last successful processing run (Phase 4).
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    processed_at: Mapped[datetime | None] = mapped_column(TZ)
    extracted_title: Mapped[str | None] = mapped_column(String(500))
    extraction_warnings: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    extracted_metadata: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class DocumentVersion(Base):
    """An immutable uploaded file. The object key is generated, never user-supplied."""

    __tablename__ = "document_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            ondelete="CASCADE",
            name="fk_document_versions_document_workspace",
        ),
        UniqueConstraint("document_id", "version_number"),
        CheckConstraint(f"format IN ({_FORMATS})", name="format_valid"),
        CheckConstraint("scan_status IN ('clean', 'not_scanned')", name="scan_status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column()
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    version_number: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    format: Mapped[str] = mapped_column(String(16))
    media_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    scan_status: Mapped[str] = mapped_column(String(16))
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class DocumentCollection(Base):
    __tablename__ = "document_collections"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_document_collections_id_workspace_id"),
        Index(
            "uq_document_collections_workspace_id_name",
            "workspace_id",
            text("lower(name)"),
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(500))
    #: Restricted (ADR 0036): only its members, and admins, see it and its documents.
    restricted: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    #: ``standard``, or ``agreement`` for the restricted collection DocuMind Sign creates
    #: for one agreement's participants (design §15 C4).
    kind: Mapped[str] = mapped_column(String(16), default="standard", server_default="standard")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class CollectionMember(Base):
    """Who may see a restricted collection (ADR 0036)."""

    __tablename__ = "collection_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "workspace_id"],
            ["document_collections.id", "document_collections.workspace_id"],
            ondelete="CASCADE",
            name="fk_collection_members_collection_workspace",
        ),
        Index("ix_collection_members_user_id", "user_id"),
    )

    collection_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    added_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class DocumentCollectionItem(Base):
    __tablename__ = "document_collection_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "workspace_id"],
            ["document_collections.id", "document_collections.workspace_id"],
            ondelete="CASCADE",
            name="fk_document_collection_items_collection_workspace",
        ),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            ondelete="CASCADE",
            name="fk_document_collection_items_document_workspace",
        ),
        Index("ix_document_collection_items_document_id", "document_id"),
    )

    collection_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    added_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
