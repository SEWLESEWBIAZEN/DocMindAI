"""API keys and chat integrations (ADR 0037). Control plane.

None of these tables hold document content: keys are stored as hashes, the
Slack bot token sits in the credential vault, and user links map a Slack user
to a DocMind account.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.infrastructure.database.base import Base

TZ = DateTime(timezone=True)


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_workspace", "workspace_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    #: The key acts as this person, within its scopes.
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(TZ)
    last_used_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)


class SlackInstallation(Base):
    __tablename__ = "slack_installations"
    __table_args__ = (CheckConstraint("status IN ('active', 'removed')", name="status_valid"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), unique=True
    )
    #: The Slack workspace ("team"). One DocMind workspace per Slack team.
    team_id: Mapped[str] = mapped_column(String(32), unique=True)
    team_name: Mapped[str | None] = mapped_column(String(200))
    bot_user_id: Mapped[str | None] = mapped_column(String(32))
    bot_token_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), default="active")
    installed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class SlackUserLink(Base):
    """A Slack user who proved, with a code from DocMind, which account they are."""

    __tablename__ = "slack_user_links"

    installation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("slack_installations.id", ondelete="CASCADE"), primary_key=True
    )
    slack_user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
