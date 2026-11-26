"""Request and response bodies for identity endpoints.

Responses are built explicitly from domain/application objects; ORM rows are
never serialised directly, so fields like password hashes can't leak.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from documind.domain.identity.permissions import RoleKey
from documind.domain.identity.policies import WorkspaceKind

# Generous transport bound; the domain enforces the real password policy.
Password = Field(min_length=1, max_length=1000)
Token = Field(min_length=16, max_length=256)


_PASSKEY = Field(
    default=None, description="The browser's WebAuthn response (PublicKeyCredential JSON)"
)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class MessageResponse(BaseModel):
    detail: str


class RegisterRequest(_In):
    email: EmailStr
    password: str = Password
    display_name: str = Field(min_length=1, max_length=200)


class LoginRequest(_In):
    email: EmailStr
    password: str = Password


class EmailRequest(_In):
    email: EmailStr


class TokenRequest(_In):
    token: str = Token


class ResetPasswordRequest(_In):
    token: str = Token
    new_password: str = Password


class ChangePasswordRequest(_In):
    current_password: str = Password
    new_password: str = Password


class ChangeEmailRequest(_In):
    new_email: EmailStr
    password: str = Password


class DeleteAccountRequest(_In):
    password: str = Password
    confirm: str = Field(max_length=20, description='Type "DELETE" to confirm.')


class DeleteOrganizationRequest(_In):
    confirm_name: str = Field(max_length=200, description="The organization's name, exactly.")


class DataExportResponse(BaseModel):
    id: uuid.UUID
    status: Literal["pending", "running", "ready", "failed", "expired"]
    size_bytes: int | None
    created_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None
    error: str | None


class DownloadLinkResponse(BaseModel):
    url: str
    expires_in: int


class ProfileUpdateRequest(_In):
    display_name: str = Field(min_length=1, max_length=200)


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    email_verified: bool
    mfa_enabled: bool
    mfa_method: Literal["passkey", "authenticator", "email"] | None = Field(
        default=None, description="Primary second factor; email codes also work as a fallback"
    )
    mfa_methods: list[Literal["passkey", "authenticator", "email", "recovery_code"]] = Field(
        default_factory=list, description="Every second factor this account can use"
    )
    default_workspace_id: uuid.UUID | None
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth2 token type, not a secret
    expires_in: int
    user: UserResponse


class WorkspaceResponse(BaseModel):
    id: uuid.UUID
    kind: WorkspaceKind
    name: str
    role: RoleKey
    organization_id: uuid.UUID | None
    organization_slug: str | None
    is_default: bool
    mfa_required_by: Literal["plan", "organization"] | None = Field(
        default=None, description="Why 2FA is required here; null when it's optional"
    )
    organization_requires_mfa: bool = Field(
        default=False, description="The organization's own setting (paid plans require 2FA anyway)"
    )
    mfa_enrollment_deadline: datetime | None = Field(
        default=None,
        description="Only for callers without 2FA: when they lose access to this workspace",
    )
    allow_email_mfa: bool = Field(
        default=True, description="Whether email codes count as 2FA in this workspace"
    )


class ActiveWorkspaceRequest(_In):
    workspace_id: uuid.UUID


class CreateOrganizationRequest(_In):
    name: str = Field(min_length=1, max_length=200)


class RenameWorkspaceRequest(_In):
    name: str = Field(min_length=1, max_length=200)


class MemberResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str
    role: RoleKey
    joined_at: datetime
    mfa_enabled: bool | None = Field(
        default=None, description="Shown only to members who can manage members"
    )
    mfa_method: Literal["passkey", "authenticator", "email"] | None = None


class MfaRequirementRequest(_In):
    required: bool | None = Field(default=None, description="Require 2FA for every member")
    allow_email_codes: bool | None = Field(
        default=None,
        description="Whether one-time codes by email count as 2FA here (else authenticator only)",
    )


class ChangeRoleRequest(_In):
    role: RoleKey


class InviteRequest(_In):
    email: EmailStr
    role: RoleKey = RoleKey.MEMBER


class InvitationResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_name: str
    email: str
    role: RoleKey
    status: str
    created_at: datetime
    expires_at: datetime


class AcceptedInvitationResponse(BaseModel):
    workspace_id: uuid.UUID


class AuditLogResponse(BaseModel):
    id: int
    created_at: datetime
    action: str
    actor_user_id: uuid.UUID | None
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    ip_address: str | None


# --- Two-factor authentication ---

MfaCode = Field(min_length=6, max_length=10, description="6-digit code from the authenticator app")


class MfaStatusResponse(BaseModel):
    enabled: bool
    method: Literal["passkey", "authenticator", "email"] | None = None
    enabled_at: datetime | None
    recovery_codes_remaining: int


class MfaRegenerateRequest(_In):
    """An authenticator code or an email code (no authenticator needed, ADR 0028)."""

    code: str | None = Field(default=None, min_length=6, max_length=10)
    email_code: str | None = Field(default=None, min_length=6, max_length=8)
    passkey: dict[str, Any] | None = _PASSKEY


class PasskeyOptionsRequest(_In):
    password: str = Password


class PasskeyRegisterRequest(_In):
    name: str = Field(min_length=1, max_length=64, description="e.g. 'MacBook Touch ID'")
    credential: dict[str, Any] = Field(description="The browser's WebAuthn registration response")


class PasskeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    backed_up: bool = Field(description="Synced passkey (e.g. iCloud Keychain, Google)")
    created_at: datetime
    last_used_at: datetime | None


class PasskeyRegisteredResponse(BaseModel):
    passkey: PasskeyResponse
    recovery_codes: list[str] | None = Field(
        default=None, description="Only when this passkey turned 2FA on"
    )


class MfaResetRequestBody(_In):
    challenge_token: str = Field(min_length=20, max_length=128)
    reason: str = Field(
        min_length=10,
        max_length=1000,
        description="What happened, so support can verify it's you",
    )


class MfaEmailConfirmRequest(_In):
    email_code: str = Field(min_length=6, max_length=8)


class MfaSetupRequest(_In):
    password: str = Password


class MfaSetupResponse(BaseModel):
    secret: str = Field(description="Base32 secret for manual entry in an authenticator app")
    otpauth_uri: str = Field(description="otpauth:// URI to render as a QR code")
    expires_at: datetime


class MfaCodeRequest(_In):
    code: str = MfaCode


_EMAIL_CODE = Field(
    default=None, min_length=6, max_length=8, description="6-digit code sent by email"
)


class MfaSecondFactorRequest(_In):
    code: str | None = Field(default=None, min_length=6, max_length=10)
    recovery_code: str | None = Field(default=None, min_length=12, max_length=32)
    email_code: str | None = _EMAIL_CODE
    passkey: dict[str, Any] | None = _PASSKEY


class MfaDisableRequest(_In):
    password: str = Password
    code: str | None = Field(default=None, min_length=6, max_length=10)
    recovery_code: str | None = Field(default=None, min_length=12, max_length=32)
    email_code: str | None = _EMAIL_CODE
    passkey: dict[str, Any] | None = _PASSKEY


class RecoveryCodesResponse(BaseModel):
    recovery_codes: list[str] = Field(description="Shown once. Store them somewhere safe.")


class MfaChallengeResponse(BaseModel):
    """The password was correct; finish signing in with ``POST /auth/login/mfa``."""

    mfa_required: Literal[True] = True
    challenge_token: str = Field(description="Single use; send it with the code")
    expires_in: int = Field(description="Seconds until the password step must be repeated")
    methods: list[Literal["passkey", "authenticator", "email", "recovery_code"]] = Field(
        default_factory=list, description="Second factors this account can use"
    )


class MfaLoginRequest(_In):
    challenge_token: str = Field(min_length=20, max_length=128)
    code: str | None = Field(default=None, min_length=6, max_length=10)
    recovery_code: str | None = Field(default=None, min_length=12, max_length=32)
    email_code: str | None = _EMAIL_CODE
    passkey: dict[str, Any] | None = _PASSKEY


class MfaChallengeTokenRequest(_In):
    challenge_token: str = Field(min_length=20, max_length=128)


class StepUpResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth2 token type, not a secret
    expires_in: int
    mfa_verified_at: datetime
