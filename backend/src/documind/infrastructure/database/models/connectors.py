"""Connectors that keep documents in sync with an outside source (ADR 0035).

``connector_connections`` is control plane: which account, which folders, the
sync cursor and the vault reference for the token. ``connector_items`` maps an
outside file to a document; it names files, so it lives in the data plane.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
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
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.infrastructure.database.base import Base

TZ = DateTime(timezone=True)
CONNECTION_STATUSES = ("needs_folders", "active", "error", "paused")


class ConnectorConnection(Base):
    __tablename__ = "connector_connections"
    __table_args__ = (
        CheckConstraint("provider IN ('google_drive')", name="provider_valid"),
        CheckConstraint(f"status IN {CONNECTION_STATUSES}", name="status_valid"),
        Index("ix_connector_connections_workspace_id", "workspace_id"),
        Index("ix_connector_connections_status_last_sync_at", "status", "last_sync_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="needs_folders")
    account_email: Mapped[str | None] = mapped_column(String(320))
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL")
    )
    #: Chosen folders: ``[{"id", "name"}]``.
    folders: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    #: The data-plane collection synced files go into.
    collection_id: Mapped[uuid.UUID | None] = mapped_column()
    #: Provider state: change cursor and known folder ids.
    cursor: Mapped[str | None] = mapped_column(Text)
    known_folders: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    files_synced: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    files_skipped: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    last_sync_at: Mapped[datetime | None] = mapped_column(TZ)
    last_error: Mapped[str | None] = mapped_column(String(64))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class ConnectorItem(Base):
    __tablename__ = "connector_items"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_id"),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            ondelete="CASCADE",
            name="fk_connector_items_document_workspace",
        ),
        Index("ix_connector_items_document_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    connection_id: Mapped[uuid.UUID] = mapped_column()
    external_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(200))
    #: md5 of the content, or the modified time for Google-native files.
    version: Mapped[str] = mapped_column(String(100))
    document_id: Mapped[uuid.UUID] = mapped_column()
    synced_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
