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


class ProfileUpdateRequest(_In):
    display_name: str = Field(min_length=1, max_length=200)


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    email_verified: bool
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
