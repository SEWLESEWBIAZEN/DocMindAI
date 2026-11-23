"""Pure authorization and ownership rules for workspace membership."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from documind.domain.errors import ConflictError, PermissionDeniedError
from documind.domain.identity.permissions import ROLE_RANK, Permission, RoleKey


class WorkspaceKind(StrEnum):
    PERSONAL = "personal"
    ORGANIZATION = "organization"


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    """The authenticated caller acting inside one workspace.

    Produced only after membership has been verified; every workspace-scoped
    service method takes one and must filter data by ``workspace_id``.
    """

    workspace_id: UUID
    workspace_kind: WorkspaceKind
    user_id: UUID
    member_id: UUID
    role: RoleKey
    permissions: frozenset[Permission]
    # Whether the caller has two-factor authentication enabled, and when they
    # last completed a 2FA check (from the access token's step-up claim).
    mfa_enabled: bool = False
    mfa_verified_at: datetime | None = None

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        if permission not in self.permissions:
            raise PermissionDeniedError("You don't have permission to do that in this workspace.")


def outranks(actor: RoleKey, target: RoleKey) -> bool:
    return ROLE_RANK[actor] > ROLE_RANK[target]


def ensure_can_assign_role(actor: RoleKey, role: RoleKey) -> None:
    """Owners may grant any role; everyone else only roles below their own."""
    if actor is RoleKey.OWNER:
        return
    if not outranks(actor, role):
        raise PermissionDeniedError("You can only assign roles below your own.")


def ensure_can_manage_member(
    ctx: WorkspaceContext, target_role: RoleKey, target_user_id: UUID
) -> None:
    """Changing or removing another member requires outranking them (owners excepted)."""
    ctx.require(Permission.MEMBERS_MANAGE)
    if target_user_id == ctx.user_id:
        raise PermissionDeniedError("Use 'leave workspace' to change your own membership.")
    if ctx.role is RoleKey.OWNER:
        return
    if not outranks(ctx.role, target_role):
        raise PermissionDeniedError("You can't manage members with an equal or higher role.")


def ensure_shared_workspace(kind: WorkspaceKind) -> None:
    if kind is WorkspaceKind.PERSONAL:
        raise ConflictError(
            "Personal workspaces can't be shared. Create an organization to collaborate.",
            code="personal_workspace",
        )


def ensure_owner_remains(current_role: RoleKey, new_role: RoleKey | None, owner_count: int) -> None:
    """An organization must always keep at least one owner.

    ``new_role`` is None when the membership is being removed.
    """
    losing_owner = current_role is RoleKey.OWNER and new_role is not RoleKey.OWNER
    if losing_owner and owner_count <= 1:
        raise ConflictError(
            "An organization needs at least one owner. Make someone else an owner first.",
            code="last_owner",
        )


def ensure_can_archive(ctx: WorkspaceContext, uploaded_by_user_id: UUID | None) -> None:
    """Admins and owners may archive anything; members only what they uploaded."""
    if ctx.can(Permission.DOCUMENTS_ARCHIVE_ANY):
        return
    if ctx.can(Permission.DOCUMENTS_ARCHIVE_OWN):
        if uploaded_by_user_id is not None and uploaded_by_user_id == ctx.user_id:
            return
        raise PermissionDeniedError("You can only archive documents you uploaded.")
    raise PermissionDeniedError("You don't have permission to do that in this workspace.")


# Tolerate small clock differences between token issuance and this check.
_MFA_CLOCK_SKEW_SECONDS = 30


def ensure_recent_mfa(ctx: WorkspaceContext, now: datetime, max_age_seconds: int) -> None:
    """Sensitive actions need 2FA enabled and verified within ``max_age_seconds``."""
    if not ctx.mfa_enabled:
        raise PermissionDeniedError(
            "Set up two-factor authentication to do this.", code="mfa_enrollment_required"
        )
    verified = ctx.mfa_verified_at
    age = (now - verified).total_seconds() if verified else None
    if age is None or age > max_age_seconds or age < -_MFA_CLOCK_SKEW_SECONDS:
        raise PermissionDeniedError(
            "Confirm it's you with your two-factor code to continue.", code="mfa_step_up_required"
        )
