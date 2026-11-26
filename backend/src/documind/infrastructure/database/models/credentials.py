"""Credential vault and workspace AI providers (ADR 0019, 0022). Control plane."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.infrastructure.database.base import Base

TZ = DateTime(timezone=True)


class Credential(Base):
    """An encrypted customer secret. Only the hint is ever shown."""

    __tablename__ = "credentials"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('ai_provider_key', 'database_password', 'storage_secret', 'worker_token', "
            "'connector_token', 'sso_client_secret')",
            name="kind_valid",
        ),
        CheckConstraint("status IN ('active', 'revoked')", name="status_valid"),
        Index("ix_credentials_workspace_id_kind", "workspace_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    ciphertext: Mapped[str] = mapped_column(Text)
    wrapped_dek: Mapped[str] = mapped_column(Text)
    kek_id: Mapped[str] = mapped_column(String(16))
    hint: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    rotated_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)
    last_used_at: Mapped[datetime | None] = mapped_column(TZ)


class WorkspaceAISettings(Base):
    __tablename__ = "workspace_ai_settings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    #: "Use my key only": never use the platform key, even when no key is set.
    key_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class WorkspaceAIProvider(Base):
    """A workspace's key for one provider, and which capabilities it serves."""

    __tablename__ = "workspace_ai_providers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "provider"),
        CheckConstraint("status IN ('active', 'invalid')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32))
    credential_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("credentials.id", ondelete="RESTRICT")
    )
    capabilities: Mapped[list[str]] = mapped_column(ARRAY(String(24)))
    #: Non-secret, provider-specific settings (models, endpoint, deployment...).
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_validated_at: Mapped[datetime | None] = mapped_column(TZ)
    invalid_since: Mapped[datetime | None] = mapped_column(TZ)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())
