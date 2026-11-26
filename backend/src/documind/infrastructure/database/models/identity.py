"""Identity and tenancy persistence models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from documind.infrastructure.database.base import Base

TZ = DateTime(timezone=True)


def _created() -> Mapped[datetime]:
    return mapped_column(TZ, server_default=func.now(), nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(TZ, server_default=func.now(), onupdate=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(email)", name="email_lowercase"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(100))
    email_verified_at: Mapped[datetime | None] = mapped_column(TZ)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    # Embedded in access tokens; bumping it invalidates all outstanding ones.
    auth_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    default_workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL", use_alter=True)
    )
    last_login_at: Mapped[datetime | None] = mapped_column(TZ)
    password_changed_at: Mapped[datetime | None] = mapped_column(TZ)
    # Two-factor (TOTP). Secrets are AES-GCM encrypted, bound to the user ID.
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    totp_enabled_at: Mapped[datetime | None] = mapped_column(TZ)
    totp_last_used_step: Mapped[int | None] = mapped_column(BigInteger)
    totp_pending_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    totp_pending_expires_at: Mapped[datetime | None] = mapped_column(TZ)
    # Email-only 2FA (ADR 0028): codes by email instead of an authenticator app.
    mfa_email_enabled_at: Mapped[datetime | None] = mapped_column(TZ)
    # Passkeys (ADR 0029): set while the user has at least one registered passkey.
    mfa_passkey_enabled_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    @property
    def mfa_enabled(self) -> bool:
        return (
            self.totp_enabled_at is not None
            or self.mfa_passkey_enabled_at is not None
            or self.mfa_email_enabled_at is not None
        )

    @property
    def mfa_method(self) -> str | None:
        """The strongest second factor: "passkey", "authenticator", "email", or None (off)."""
        if self.mfa_passkey_enabled_at is not None:
            return "passkey"
        if self.totp_enabled_at is not None:
            return "authenticator"
        if self.mfa_email_enabled_at is not None:
            return "email"
        return None

    @property
    def mfa_methods(self) -> list[str]:
        """Everything usable at a 2FA prompt (email codes are always a fallback)."""
        if not self.mfa_enabled:
            return []
        methods = []
        if self.mfa_passkey_enabled_at is not None:
            methods.append("passkey")
        if self.totp_enabled_at is not None:
            methods.append("authenticator")
        return [*methods, "email", "recovery_code"]

    def mfa_allowed_in(self, allow_email: bool) -> bool:
        """Whether this user's 2FA counts where email codes may or may not be used."""
        return (
            self.totp_enabled_at is not None
            or self.mfa_passkey_enabled_at is not None
            or (allow_email and self.mfa_email_enabled_at is not None)
        )


class MfaRecoveryCode(Base):
    """Single-use 2FA recovery codes, stored as keyed (HMAC-SHA256) hashes."""

    __tablename__ = "mfa_recovery_codes"
    __table_args__ = (UniqueConstraint("user_id", "code_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(String(64))
    used_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _created()


class WebAuthnCredential(Base):
    """A registered passkey (ADR 0029). Only the public key is stored."""

    __tablename__ = "webauthn_credentials"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    transports: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), default=list, server_default=text("'{}'")
    )
    name: Mapped[str] = mapped_column(String(64))
    backed_up: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = _created()
    last_used_at: Mapped[datetime | None] = mapped_column(TZ)


class MfaResetRequest(Base):
    """Support-assisted 2FA reset for someone who lost every factor (ADR 0030)."""

    __tablename__ = "mfa_reset_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'cancelled', 'completed')",
            name="status_valid",
        ),
        Index("ix_mfa_reset_requests_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    reason: Mapped[str] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = _created()
    decided_at: Mapped[datetime | None] = mapped_column(TZ)
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decision_note: Mapped[str | None] = mapped_column(String(500))
    effective_at: Mapped[datetime | None] = mapped_column(TZ)
    completed_at: Mapped[datetime | None] = mapped_column(TZ)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Workspace(Base):
    """The tenant boundary. Either one user's personal space or an organization's."""

    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("kind IN ('personal', 'organization')", name="kind_valid"),
        CheckConstraint(
            "(kind = 'personal' AND personal_owner_id IS NOT NULL AND organization_id IS NULL)"
            " OR (kind = 'organization' AND organization_id IS NOT NULL"
            " AND personal_owner_id IS NULL)",
            name="kind_owner_consistent",
        ),
        CheckConstraint(
            "voice_policy IN ('enabled', 'browser', 'disabled')", name="voice_policy_valid"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))
    # ADR 0011: "enabled" allows DocuMind's speech service (audio is sent to the
    # speech provider, never stored); "browser" allows only the browser's own
    # speech features; "disabled" turns voice off for the workspace.
    voice_policy: Mapped[str] = mapped_column(
        String(16), default="enabled", server_default="enabled"
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), unique=True
    )
    personal_owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    # ADR 0026: an organization can require 2FA on any plan; paid plans always do.
    mfa_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # When 2FA became required here (start of the members' grace period). Set on
    # first use while required, cleared when it no longer is.
    mfa_enforced_since: Mapped[datetime | None] = mapped_column(TZ)
    # ADR 0028: whether one-time codes by email count as 2FA here (else authenticator only).
    allow_email_mfa: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    organization: Mapped[Organization | None] = relationship(lazy="raise")


role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    rank: Mapped[int] = mapped_column(SmallInteger)

    permissions: Mapped[list[Permission]] = relationship(secondary=role_permissions, lazy="raise")


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(Text)


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id"),
        Index("ix_workspace_members_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    role: Mapped[Role] = relationship(lazy="raise")
    user: Mapped[User] = relationship(lazy="raise")
    workspace: Mapped[Workspace] = relationship(lazy="raise")


class RefreshToken(Base):
    """One row per issued refresh token; only a SHA-256 hash of the token is stored."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_family_id", "family_id"),
        Index("ix_refresh_tokens_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[uuid.UUID] = mapped_column()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_reason: Mapped[str | None] = mapped_column(String(32))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    #: Signed in with this organization's SSO (ADR 0036); kept across refreshes.
    sso_workspace_id: Mapped[uuid.UUID | None] = mapped_column()


class AccountToken(Base):
    """Single-use tokens: email links, the 2FA sign-in challenge and email codes (hash only)."""

    __tablename__ = "account_tokens"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('email_verification', 'password_reset', 'mfa_login', 'mfa_email', "
            "'webauthn_reg', 'webauthn_auth', 'email_change')",
            name="purpose_valid",
        ),
        Index("ix_account_tokens_user_id_purpose", "user_id", "purpose"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(TZ)
    used_at: Mapped[datetime | None] = mapped_column(TZ)
    #: The address an ``email_change`` token moves the account to (ADR 0039).
    new_email: Mapped[str | None] = mapped_column(String(320))


class DataExport(Base):
    """A "download my data" archive (ADR 0039), kept for a few days in private storage."""

    __tablename__ = "data_exports"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'ready', 'failed', 'expired')", name="status_valid"
        ),
        Index("ix_data_exports_user_id_created_at", "user_id", "created_at"),
        Index("ix_data_exports_status_expires_at", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    object_key: Mapped[str | None] = mapped_column(String(300))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = _created()
    completed_at: Mapped[datetime | None] = mapped_column(TZ)
    expires_at: Mapped[datetime | None] = mapped_column(TZ)


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'revoked')", name="status_valid"
        ),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        Index(
            "uq_invitations_pending_workspace_email",
            "workspace_id",
            "email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_invitations_email", "email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(320))
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="RESTRICT"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(TZ)
    responded_at: Mapped[datetime | None] = mapped_column(TZ)

    role: Mapped[Role] = relationship(lazy="raise")
    workspace: Mapped[Workspace] = relationship(lazy="raise")


class AuditLog(Base):
    """Append-only audit trail.

    Workspace and actor IDs deliberately have no foreign keys so the trail
    survives deletion of the workspace or user it refers to.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_workspace_id_created_at", "workspace_id", "created_at"),
        Index("ix_audit_logs_actor_user_id_created_at", "actor_user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = _created()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column()
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column()
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    request_id: Mapped[str | None] = mapped_column(String(128))
    #: Set for entries relayed from a dedicated or external data plane's outbox,
    #: so a relay that runs twice never duplicates an entry (ADR 0021).
    event_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)
