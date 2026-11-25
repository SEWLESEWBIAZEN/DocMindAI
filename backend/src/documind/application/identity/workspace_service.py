"""Workspace, organization and membership use cases.

Every method that operates inside a workspace takes a ``WorkspaceContext``,
which only exists after membership was verified. Lookups are always scoped to
``ctx.workspace_id``; an ID from another workspace is reported as not found.
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity.common import AuditRecorder, RequestMeta, utcnow
from documind.domain.errors import NotFoundError
from documind.domain.identity.permissions import Permission, RoleKey
from documind.domain.identity.policies import (
    MfaRequiredBy,
    WorkspaceContext,
    WorkspaceKind,
    ensure_can_assign_role,
    ensure_can_manage_member,
    ensure_mfa_policy_met,
    ensure_owner_remains,
    ensure_recent_mfa,
    ensure_shared_workspace,
    mfa_enrollment_deadline,
    mfa_required_by,
)
from documind.domain.identity.values import clean_display_name
from documind.infrastructure.database.models import (
    AuditLog,
    Organization,
    User,
    Workspace,
    WorkspaceMember,
    WorkspacePlan,
)
from documind.infrastructure.database.repositories.identity import (
    AuditLogRepository,
    MembershipView,
    RoleRepository,
    UserRepository,
    WorkspaceRepository,
)

logger = logging.getLogger(__name__)

WORKSPACE_NOT_FOUND = "Workspace not found."


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    id: uuid.UUID
    kind: WorkspaceKind
    name: str
    role: RoleKey
    organization_id: uuid.UUID | None
    organization_slug: str | None
    is_default: bool
    # ADR 0026: why 2FA is required here (None: optional), and for a caller
    # without 2FA, when they lose access.
    mfa_required_by: MfaRequiredBy | None = None
    organization_requires_mfa: bool = False
    mfa_enrollment_deadline: datetime | None = None
    allow_email_mfa: bool = True


@dataclass(frozen=True, slots=True)
class MemberInfo:
    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str
    role: RoleKey
    joined_at: datetime
    mfa_enabled: bool | None = None  # only shown to members who manage members
    mfa_method: str | None = None


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "org"
    return f"{base}-{secrets.token_hex(3)}"


class WorkspaceService:
    def __init__(
        self,
        session: AsyncSession,
        meta: RequestMeta,
        *,
        mfa_grace_days: int = 14,
        mfa_step_up_seconds: int = 300,
    ) -> None:
        self._session = session
        self._grace_days = mfa_grace_days
        self._step_up_seconds = mfa_step_up_seconds
        self._users = UserRepository(session)
        self._roles = RoleRepository(session)
        self._workspaces = WorkspaceRepository(session)
        self._audit_repo = AuditLogRepository(session)
        self._audit = AuditRecorder(self._audit_repo, meta)

    # ------------------------------------------------------------- resolution

    async def resolve_context(
        self,
        user: User,
        workspace_id: uuid.UUID,
        *,
        mfa_verified_at: datetime | None = None,
        mfa_method: str | None = None,
        enforce_mfa: bool = True,
    ) -> WorkspaceContext:
        """Authorize entry into a workspace. Non-members get 'not found', never 'forbidden'.

        Members without 2FA are refused once a workspace that requires it is past
        their grace period (``enforce_mfa=False`` only for reading it and leaving).
        """
        view = await self._workspaces.membership(workspace_id, user.id)
        if view is None:
            raise NotFoundError(WORKSPACE_NOT_FOUND)
        ctx = await self._context(user, view, mfa_verified_at, mfa_method)
        if enforce_mfa:
            ensure_mfa_policy_met(ctx, utcnow())
        return ctx

    async def _plan_keys(self, workspace_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not workspace_ids:
            return {}
        rows = await self._session.execute(
            select(WorkspacePlan.workspace_id, WorkspacePlan.plan_key).where(
                WorkspacePlan.workspace_id.in_(workspace_ids)
            )
        )
        return {w: str(k) for w, k in rows.all()}

    async def _sync_mfa_requirement(self, workspace: Workspace, required: bool) -> None:
        """Start the grace period the first time a requirement is seen; reset it when lifted."""
        if required == (workspace.mfa_enforced_since is not None):
            return
        value = utcnow() if required else None
        condition = (
            Workspace.mfa_enforced_since.is_(None)
            if required
            else Workspace.mfa_enforced_since.is_not(None)
        )
        await self._session.execute(
            update(Workspace)
            .where(Workspace.id == workspace.id, condition)
            .values(mfa_enforced_since=value)
        )
        await self._session.commit()
        await self._session.refresh(workspace, ["mfa_enforced_since"])

    def _deadline(self, workspace: Workspace, user: User, required: bool) -> datetime | None:
        if not required or user.mfa_allowed_in(workspace.allow_email_mfa):
            return None
        return mfa_enrollment_deadline(workspace.mfa_enforced_since, utcnow(), self._grace_days)

    async def _context(
        self,
        user: User,
        view: MembershipView,
        mfa_verified_at: datetime | None,
        mfa_method: str | None = None,
    ) -> WorkspaceContext:
        keys = await self._roles.permission_keys(view.role.id)
        known = {p.value for p in Permission}
        unknown = keys - known
        if unknown:
            logger.warning(
                "Ignoring permissions unknown to this build", extra={"keys": sorted(unknown)}
            )
        workspace = view.workspace
        plan = (await self._plan_keys([workspace.id])).get(workspace.id)
        required = mfa_required_by(plan, workspace.mfa_required) is not None
        await self._sync_mfa_requirement(workspace, required)
        return WorkspaceContext(
            workspace_id=workspace.id,
            workspace_kind=WorkspaceKind(workspace.kind),
            user_id=user.id,
            member_id=view.member.id,
            role=RoleKey(view.role.key),
            permissions=frozenset(Permission(k) for k in keys & known),
            # ADR 0028: where email codes aren't allowed, only an authenticator counts,
            # and a step-up done with an email code doesn't either.
            mfa_enabled=user.mfa_allowed_in(workspace.allow_email_mfa),
            mfa_verified_at=None
            if mfa_method == "email_code" and not workspace.allow_email_mfa
            else mfa_verified_at,
            mfa_required=required,
            mfa_enrollment_deadline=self._deadline(workspace, user, required),
        )

    # -------------------------------------------------------------- listing

    def _summary(
        self, view: MembershipView, user: User, org: Organization | None, plan: str | None
    ) -> WorkspaceSummary:
        workspace = view.workspace
        by = mfa_required_by(plan, workspace.mfa_required)
        return WorkspaceSummary(
            id=workspace.id,
            kind=WorkspaceKind(workspace.kind),
            name=workspace.name,
            role=RoleKey(view.role.key),
            organization_id=workspace.organization_id,
            organization_slug=org.slug if org else None,
            is_default=user.default_workspace_id == workspace.id,
            mfa_required_by=by,
            organization_requires_mfa=workspace.mfa_required,
            mfa_enrollment_deadline=self._deadline(workspace, user, by is not None),
            allow_email_mfa=workspace.allow_email_mfa,
        )

    async def list_for_user(self, user: User) -> list[WorkspaceSummary]:
        views = await self._workspaces.memberships_for_user(user.id)
        plans = await self._plan_keys([v.workspace.id for v in views])
        summaries = []
        for view in views:
            workspace = await self._workspaces.get(view.workspace.id)
            summaries.append(
                self._summary(
                    view,
                    user,
                    workspace.organization if workspace else None,
                    plans.get(view.workspace.id),
                )
            )
        return summaries

    async def get(self, ctx: WorkspaceContext, user: User) -> WorkspaceSummary:
        ctx.require(Permission.WORKSPACE_READ)
        view = await self._workspaces.membership(ctx.workspace_id, user.id)
        workspace = await self._workspaces.get(ctx.workspace_id)
        if view is None or workspace is None:
            raise NotFoundError(WORKSPACE_NOT_FOUND)
        plan = (await self._plan_keys([workspace.id])).get(workspace.id)
        return self._summary(view, user, workspace.organization, plan)

    async def set_default(self, user: User, workspace_id: uuid.UUID) -> None:
        """Workspace switching: remember the active workspace for this user."""
        if not await self._workspaces.is_member(workspace_id, user.id):
            raise NotFoundError(WORKSPACE_NOT_FOUND)
        locked = await self._users.require(user.id, for_update=True)
        locked.default_workspace_id = workspace_id
        await self._session.commit()

    # ---------------------------------------------------------- organizations

    async def create_organization(self, user: User, name: str) -> WorkspaceSummary:
        name = clean_display_name(name)
        org = Organization(
            id=uuid.uuid4(), name=name, slug=_slugify(name), created_by_user_id=user.id
        )
        workspace = Workspace(
            id=uuid.uuid4(),
            kind=WorkspaceKind.ORGANIZATION.value,
            name=name,
            organization_id=org.id,
        )
        owner = await self._roles.get_by_key(RoleKey.OWNER)
        self._workspaces.add(org)
        await self._session.flush()
        self._workspaces.add(workspace)
        await self._session.flush()
        self._workspaces.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role_id=owner.id)
        )
        self._audit.record(
            "organization.created",
            actor_user_id=user.id,
            workspace_id=workspace.id,
            target_type="organization",
            target_id=org.id,
            details={"name": name},
        )
        await self._session.commit()
        return WorkspaceSummary(
            id=workspace.id,
            kind=WorkspaceKind.ORGANIZATION,
            name=name,
            role=RoleKey.OWNER,
            organization_id=org.id,
            organization_slug=org.slug,
            is_default=False,
        )

    async def rename(self, ctx: WorkspaceContext, user: User, name: str) -> WorkspaceSummary:
        ctx.require(Permission.WORKSPACE_UPDATE)
        name = clean_display_name(name)
        workspace = await self._workspaces.get(ctx.workspace_id)
        if workspace is None:
            raise NotFoundError(WORKSPACE_NOT_FOUND)
        old = workspace.name
        workspace.name = name
        if workspace.organization is not None:
            workspace.organization.name = name
        self._audit.record(
            "workspace.renamed",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="workspace",
            target_id=ctx.workspace_id,
            details={"from": old, "to": name},
        )
        await self._session.commit()
        return await self.get(ctx, user)

    # --------------------------------------------------------------- members

    async def list_members(self, ctx: WorkspaceContext) -> list[MemberInfo]:
        ctx.require(Permission.MEMBERS_READ)
        # Who still has to set up 2FA is for the people who manage members.
        show_mfa = ctx.can(Permission.MEMBERS_MANAGE)
        allow_email = (await self._workspace(ctx.workspace_id)).allow_email_mfa
        return [
            MemberInfo(
                id=m.id,
                user_id=m.user_id,
                email=m.user.email,
                display_name=m.user.display_name,
                role=RoleKey(m.role.key),
                joined_at=m.created_at,
                # Counted as this workspace counts it (email may not be allowed here).
                mfa_enabled=m.user.mfa_allowed_in(allow_email) if show_mfa else None,
                mfa_method=m.user.mfa_method if show_mfa else None,
            )
            for m in await self._workspaces.list_members(ctx.workspace_id)
        ]

    async def _workspace(self, workspace_id: uuid.UUID) -> Workspace:
        return (
            await self._session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()

    async def set_mfa_policy(
        self,
        ctx: WorkspaceContext,
        user: User,
        *,
        required: bool | None = None,
        allow_email: bool | None = None,
    ) -> WorkspaceSummary:
        """Organization 2FA settings (ADR 0026, 0028): require it, and whether email counts.

        Needs a fresh 2FA check by an admin.
        """
        ctx.require(Permission.WORKSPACE_UPDATE)
        ensure_shared_workspace(ctx.workspace_kind)
        ensure_recent_mfa(ctx, utcnow(), self._step_up_seconds)
        workspace = (
            await self._session.execute(
                select(Workspace).where(Workspace.id == ctx.workspace_id).with_for_update()
            )
        ).scalar_one()
        changes: dict[str, bool] = {}
        if required is not None and workspace.mfa_required != required:
            workspace.mfa_required = required
            changes["required"] = required
        if allow_email is not None and workspace.allow_email_mfa != allow_email:
            workspace.allow_email_mfa = allow_email
            changes["allow_email_codes"] = allow_email
        if changes:
            self._audit.record(
                "workspace.mfa_requirement_changed",
                actor_user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                target_type="workspace",
                target_id=ctx.workspace_id,
                details=changes,
            )
            await self._session.commit()
            plan = (await self._plan_keys([workspace.id])).get(workspace.id)
            await self._sync_mfa_requirement(
                workspace, mfa_required_by(plan, workspace.mfa_required) is not None
            )
        return await self.get(ctx, user)

    async def change_role(
        self, ctx: WorkspaceContext, member_id: uuid.UUID, new_role: RoleKey
    ) -> None:
        ensure_shared_workspace(ctx.workspace_kind)
        target = await self._workspaces.get_member(ctx.workspace_id, member_id, for_update=True)
        if target is None:
            raise NotFoundError("Member not found.")
        current = RoleKey(target.role.key)
        ensure_can_manage_member(ctx, current, target.user_id)
        ensure_can_assign_role(ctx.role, new_role)
        if current is new_role:
            return
        owner = await self._roles.get_by_key(RoleKey.OWNER)
        owners = await self._workspaces.lock_and_count_owners(ctx.workspace_id, owner.id)
        ensure_owner_remains(current, new_role, owners)
        role = await self._roles.get_by_key(new_role)
        target.role_id = role.id
        self._audit.record(
            "member.role_changed",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="member",
            target_id=target.id,
            details={"user_id": str(target.user_id), "from": current.value, "to": new_role.value},
        )
        await self._session.commit()

    async def remove_member(self, ctx: WorkspaceContext, member_id: uuid.UUID) -> None:
        ensure_shared_workspace(ctx.workspace_kind)
        target = await self._workspaces.get_member(ctx.workspace_id, member_id, for_update=True)
        if target is None:
            raise NotFoundError("Member not found.")
        current = RoleKey(target.role.key)
        ensure_can_manage_member(ctx, current, target.user_id)
        owner = await self._roles.get_by_key(RoleKey.OWNER)
        owners = await self._workspaces.lock_and_count_owners(ctx.workspace_id, owner.id)
        ensure_owner_remains(current, None, owners)
        await self._detach(target)
        self._audit.record(
            "member.removed",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="member",
            target_id=target.id,
            details={"user_id": str(target.user_id), "role": current.value},
        )
        await self._session.commit()

    async def leave(self, ctx: WorkspaceContext) -> None:
        ensure_shared_workspace(ctx.workspace_kind)
        me = await self._workspaces.get_member(ctx.workspace_id, ctx.member_id, for_update=True)
        if me is None:
            raise NotFoundError(WORKSPACE_NOT_FOUND)
        owner = await self._roles.get_by_key(RoleKey.OWNER)
        owners = await self._workspaces.lock_and_count_owners(ctx.workspace_id, owner.id)
        ensure_owner_remains(ctx.role, None, owners)
        await self._detach(me)
        self._audit.record(
            "member.left",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="member",
            target_id=me.id,
            details={"role": ctx.role.value},
        )
        await self._session.commit()

    async def _detach(self, member: WorkspaceMember) -> None:
        """Remove a membership; fall back to the personal workspace if it was the default."""
        user = await self._users.get(member.user_id, for_update=True)
        if user is not None and user.default_workspace_id == member.workspace_id:
            personal = await self._workspaces.get_personal(user.id)
            user.default_workspace_id = personal.id if personal else None
        await self._workspaces.delete_member(member)

    # ------------------------------------------------------------------ audit

    async def audit_log(
        self, ctx: WorkspaceContext, *, limit: int, before_id: int | None
    ) -> list[AuditLog]:
        ctx.require(Permission.AUDIT_READ)
        return await self._audit_repo.list_for_workspace(
            ctx.workspace_id, limit=min(max(limit, 1), 100), before_id=before_id
        )
