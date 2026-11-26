"""Pure authorization and ownership rules for workspace membership."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal
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
    # ADR 0026: whether this workspace requires 2FA, and (for callers without it)
    # when their grace period ends.
    mfa_required: bool = False
    mfa_enrollment_deadline: datetime | None = None
    # ADR 0037: answers posted where others can read them (a Slack channel) use only
    # documents everyone in the workspace may see.
    public_only: bool = False

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


# --------------------------------------------------------------------------- #
# Required 2FA (ADR 0026)
# --------------------------------------------------------------------------- #

MfaRequiredBy = Literal["plan", "organization"]


def mfa_required_by(plan_key: str | None, organization_requires: bool) -> MfaRequiredBy | None:
    """Paid plans always require 2FA; any organization may require it on any plan."""
    if plan_key not in (None, "free"):
        return "plan"
    if organization_requires:
        return "organization"
    return None


def mfa_enrollment_deadline(
    required_since: datetime | None, now: datetime, grace_days: int
) -> datetime:
    """When members without 2FA lose access (grace counted from when it became required)."""
    return (required_since or now) + timedelta(days=grace_days)


def ensure_mfa_policy_met(ctx: WorkspaceContext, now: datetime) -> None:
    """Members without 2FA are refused once the workspace's grace period has ended."""
    if (
        ctx.mfa_required
        and not ctx.mfa_enabled
        and ctx.mfa_enrollment_deadline is not None
        and now >= ctx.mfa_enrollment_deadline
    ):
        raise PermissionDeniedError(
            "This workspace requires two-factor authentication. Set it up under "
            "Profile, then try again.",
            code="workspace_requires_mfa",
        )
