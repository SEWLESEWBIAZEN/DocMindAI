"""Infrastructure registry (ADR 0021). Control plane."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.infrastructure.database.base import Base

TZ = DateTime(timezone=True)


class InfrastructureProfile(Base):
    """One per organization workspace; its versions are immutable."""

    __tablename__ = "infrastructure_profiles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), unique=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class InfrastructureProfileVersion(Base):
    __tablename__ = "infrastructure_profile_versions"
    __table_args__ = (
        UniqueConstraint("profile_id", "version"),
        CheckConstraint(
            "status IN ('draft', 'validated', 'failed', 'active', 'retired')", name="status_valid"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("infrastructure_profiles.id", ondelete="CASCADE")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    #: Non-secret configuration; secrets are credential ids (ADR 0022).
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    #: SHA-256 of the canonical configuration, verified whenever it is loaded.
    checksum: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    validation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    note: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    validated_at: Mapped[datetime | None] = mapped_column(TZ)
    activated_at: Mapped[datetime | None] = mapped_column(TZ)
    activated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class WorkspaceInfrastructure(Base):
    """Pins a workspace to one active profile version. No row: platform default."""

    __tablename__ = "workspace_infrastructure"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("infrastructure_profile_versions.id", ondelete="RESTRICT")
    )
    assigned_at: Mapped[datetime] = mapped_column(
        TZ, server_default=func.now(), onupdate=func.now()
    )


class InfrastructureEvent(Base):
    """Validation, activation, health and migration history (MI-8 monitoring)."""

    __tablename__ = "infrastructure_events"
    __table_args__ = (
        Index("ix_infrastructure_events_workspace_id_created_at", "workspace_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    version_id: Mapped[uuid.UUID | None] = mapped_column()
    kind: Mapped[str] = mapped_column(String(48))
    ok: Mapped[bool | None] = mapped_column()
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class WorkerRegistration(Base):
    """Customer-hosted processing workers (MI-7 foundation). Tokens are hashed."""

    __tablename__ = "worker_registrations"
    __table_args__ = (CheckConstraint("status IN ('active', 'revoked')", name="status_valid"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    token_prefix: Mapped[str] = mapped_column(String(16))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)


class InfrastructureMigration(Base):
    """A move between data planes or storage (MI-8, ADR 0025).

    planned → approved (owner, 2FA) → running → completed or failed. Data only
    moves after approval; the source copy is kept until an explicit cleanup.
    """

    __tablename__ = "infrastructure_migrations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('planned', 'approved', 'running', 'completed', 'failed', 'cancelled')",
            name="status_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    from_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("infrastructure_profile_versions.id", ondelete="SET NULL")
    )
    to_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("infrastructure_profile_versions.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16), default="planned", server_default="planned")
    plan: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())
