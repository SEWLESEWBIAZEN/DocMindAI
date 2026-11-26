"""Single sign-on (OIDC) and SCIM provisioning per organization (ADR 0036). Control plane."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.infrastructure.database.base import Base

TZ = DateTime(timezone=True)


class SsoConnection(Base):
    __tablename__ = "sso_connections"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_verification', 'active', 'disabled')", name="status_valid"
        ),
        CheckConstraint("default_role IN ('member', 'viewer')", name="default_role_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), unique=True
    )
    issuer: Mapped[str] = mapped_column(String(500))
    client_id: Mapped[str] = mapped_column(String(300))
    client_secret_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL")
    )
    #: Email domains this organization signs in for, lowercase.
    domains: Mapped[list[str]] = mapped_column(JSONB)
    default_role: Mapped[str] = mapped_column(String(16), default="member", server_default="member")
    #: Members with these domains must use SSO (no password sign-in).
    enforced: Mapped[bool] = mapped_column(default=False, server_default=sql_text("false"))
    #: An operator confirms the organization owns the domains before SSO works.
    status: Mapped[str] = mapped_column(String(24), default="pending_verification")
    verified_by: Mapped[str | None] = mapped_column(String(128))
    verification_note: Mapped[str | None] = mapped_column(String(500))
    scim_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    scim_token_prefix: Mapped[str | None] = mapped_column(String(16))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())
