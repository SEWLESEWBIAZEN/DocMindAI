"""Identity and tenancy repositories.

Rules every method follows:

* Anything that belongs to a workspace is looked up with ``workspace_id`` as a
  required argument and filtered in SQL, so an ID from another tenant simply
  isn't found (no IDOR, no existence leak).
* Rows that a use case reads and then changes under a business invariant
  (token rotation, last-owner checks, invitation acceptance) are locked with
  ``SELECT ... FOR UPDATE``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager, joinedload

from documind.domain.errors import AuthenticationError
from documind.infrastructure.database.models import (
    AccountToken,
    AuditLog,
    Invitation,
    Organization,
    Permission,
    RefreshToken,
    Role,
    User,
    Workspace,
    WorkspaceMember,
    role_permissions,
)


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, user_id: uuid.UUID, *, for_update: bool = False) -> User | None:
        stmt = select(User).where(User.id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def require(self, user_id: uuid.UUID, *, for_update: bool = False) -> User:
        """Load a user that must exist (e.g. the authenticated caller)."""
        user = await self.get(user_id, for_update=for_update)
        if user is None:
            raise AuthenticationError("Your session is no longer valid. Please sign in again.")
        return user

    async def get_by_email(self, email: str) -> User | None:
        return (await self._s.execute(select(User).where(User.email == email))).scalar_one_or_none()

    def add(self, user: User) -> None:
        self._s.add(user)


class RoleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_by_key(self, key: str) -> Role:
        return (await self._s.execute(select(Role).where(Role.key == key))).scalar_one()

    async def permission_keys(self, role_id: int) -> set[str]:
        stmt = (
            select(Permission.key)
            .join(role_permissions, role_permissions.c.permission_id == Permission.id)
            .where(role_permissions.c.role_id == role_id)
        )
        return set((await self._s.execute(stmt)).scalars())

    async def all_with_permissions(self) -> dict[str, set[str]]:
        stmt = (
            select(Role.key, Permission.key)
            .join(role_permissions, role_permissions.c.role_id == Role.id)
            .join(Permission, Permission.id == role_permissions.c.permission_id)
        )
        result: dict[str, set[str]] = {}
        for role_key, permission_key in await self._s.execute(stmt):
            result.setdefault(role_key, set()).add(permission_key)
        return result


@dataclass(frozen=True, slots=True)
class MembershipView:
    member: WorkspaceMember
    workspace: Workspace
    role: Role


class WorkspaceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    def add(self, *objects: Workspace | Organization | WorkspaceMember) -> None:
        self._s.add_all(objects)

    async def get(self, workspace_id: uuid.UUID) -> Workspace | None:
        stmt = (
            select(Workspace)
            .options(joinedload(Workspace.organization))
            .where(Workspace.id == workspace_id)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_personal(self, user_id: uuid.UUID) -> Workspace | None:
        stmt = select(Workspace).where(Workspace.personal_owner_id == user_id)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def membership(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> MembershipView | None:
        stmt = (
            select(WorkspaceMember, Workspace, Role)
            .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
            .join(Role, Role.id == WorkspaceMember.role_id)
            .where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id)
        )
        row = (await self._s.execute(stmt)).one_or_none()
        return MembershipView(member=row[0], workspace=row[1], role=row[2]) if row else None

    async def memberships_for_user(self, user_id: uuid.UUID) -> list[MembershipView]:
        stmt = (
            select(WorkspaceMember, Workspace, Role)
            .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
            .join(Role, Role.id == WorkspaceMember.role_id)
            .where(WorkspaceMember.user_id == user_id)
            .order_by(Workspace.kind.desc(), Workspace.name)  # personal first
        )
        return [MembershipView(m, w, r) for m, w, r in await self._s.execute(stmt)]

    async def is_member(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        stmt = select(WorkspaceMember.id).where(
            WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id
        )
        return (await self._s.execute(stmt)).first() is not None

    async def member_by_email(self, workspace_id: uuid.UUID, email: str) -> WorkspaceMember | None:
        stmt = (
            select(WorkspaceMember)
            .join(User, User.id == WorkspaceMember.user_id)
            .where(WorkspaceMember.workspace_id == workspace_id, User.email == email)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_member(
        self, workspace_id: uuid.UUID, member_id: uuid.UUID, *, for_update: bool = False
    ) -> WorkspaceMember | None:
        stmt = (
            select(WorkspaceMember)
            .join(WorkspaceMember.role)
            .options(contains_eager(WorkspaceMember.role))
            .where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.id == member_id)
        )
        if for_update:
            stmt = stmt.with_for_update(of=WorkspaceMember)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_members(self, workspace_id: uuid.UUID) -> list[WorkspaceMember]:
        stmt = (
            select(WorkspaceMember)
            .join(WorkspaceMember.user)
            .join(WorkspaceMember.role)
            .options(contains_eager(WorkspaceMember.user), contains_eager(WorkspaceMember.role))
            .where(WorkspaceMember.workspace_id == workspace_id)
            .order_by(Role.rank.desc(), User.display_name)
        )
        return list((await self._s.execute(stmt)).scalars())

    async def lock_and_count_owners(self, workspace_id: uuid.UUID, owner_role_id: int) -> int:
        """Lock the owner rows so concurrent demotions can't both pass the last-owner check."""
        stmt = (
            select(WorkspaceMember.id)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role_id == owner_role_id,
            )
            .with_for_update()
        )
        return len((await self._s.execute(stmt)).all())

    async def delete_member(self, member: WorkspaceMember) -> None:
        await self._s.delete(member)


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    def add(self, token: RefreshToken) -> None:
        self._s.add(token)

    async def get_by_hash(
        self, token_hash: str, *, for_update: bool = False
    ) -> RefreshToken | None:
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def revoke_family(self, family_id: uuid.UUID, reason: str, now: datetime) -> None:
        await self._s.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
        )

    async def revoke_all_for_user(self, user_id: uuid.UUID, reason: str, now: datetime) -> None:
        await self._s.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
        )


class AccountTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    def add(self, token: AccountToken) -> None:
        self._s.add(token)

    async def get_by_hash(self, token_hash: str, purpose: str) -> AccountToken | None:
        stmt = (
            select(AccountToken)
            .where(AccountToken.token_hash == token_hash, AccountToken.purpose == purpose)
            .with_for_update()
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def invalidate_unused(self, user_id: uuid.UUID, purpose: str, now: datetime) -> None:
        await self._s.execute(
            update(AccountToken)
            .where(
                AccountToken.user_id == user_id,
                AccountToken.purpose == purpose,
                AccountToken.used_at.is_(None),
            )
            .values(used_at=now)
        )


class InvitationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    def add(self, invitation: Invitation) -> None:
        self._s.add(invitation)

    def _with_relations(self) -> Any:
        return (
            select(Invitation)
            .join(Invitation.role)
            .join(Invitation.workspace)
            .options(contains_eager(Invitation.role), contains_eager(Invitation.workspace))
        )

    async def get(
        self, workspace_id: uuid.UUID, invitation_id: uuid.UUID, *, for_update: bool = False
    ) -> Invitation | None:
        stmt = self._with_relations().where(
            Invitation.workspace_id == workspace_id, Invitation.id == invitation_id
        )
        if for_update:
            stmt = stmt.with_for_update(of=Invitation)
        result: Invitation | None = (await self._s.execute(stmt)).scalar_one_or_none()
        return result

    async def get_for_email(
        self, invitation_id: uuid.UUID, email: str, *, for_update: bool = False
    ) -> Invitation | None:
        stmt = self._with_relations().where(
            Invitation.id == invitation_id, Invitation.email == email
        )
        if for_update:
            stmt = stmt.with_for_update(of=Invitation)
        result: Invitation | None = (await self._s.execute(stmt)).scalar_one_or_none()
        return result

    async def get_by_token_hash(self, token_hash: str) -> Invitation | None:
        stmt = self._with_relations().where(Invitation.token_hash == token_hash)
        result: Invitation | None = (
            await self._s.execute(stmt.with_for_update(of=Invitation))
        ).scalar_one_or_none()
        return result

    async def pending_for_workspace_email(
        self, workspace_id: uuid.UUID, email: str
    ) -> Invitation | None:
        stmt = (
            select(Invitation)
            .where(
                Invitation.workspace_id == workspace_id,
                Invitation.email == email,
                Invitation.status == "pending",
            )
            .with_for_update()
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_pending_for_workspace(self, workspace_id: uuid.UUID) -> list[Invitation]:
        stmt = (
            self._with_relations()
            .where(Invitation.workspace_id == workspace_id, Invitation.status == "pending")
            .order_by(Invitation.created_at.desc())
        )
        return list((await self._s.execute(stmt)).scalars())

    async def list_pending_for_email(self, email: str, now: datetime) -> list[Invitation]:
        stmt = (
            self._with_relations()
            .where(
                Invitation.email == email,
                Invitation.status == "pending",
                Invitation.expires_at > now,
            )
            .order_by(Invitation.created_at.desc())
        )
        return list((await self._s.execute(stmt)).scalars())


class AuditLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    def add(self, entry: AuditLog) -> None:
        self._s.add(entry)

    async def list_for_workspace(
        self, workspace_id: uuid.UUID, *, limit: int, before_id: int | None
    ) -> list[AuditLog]:
        stmt = select(AuditLog).where(AuditLog.workspace_id == workspace_id)
        if before_id is not None:
            stmt = stmt.where(AuditLog.id < before_id)
        stmt = stmt.order_by(AuditLog.id.desc()).limit(limit)
        return list((await self._s.execute(stmt)).scalars())

    async def count(self, **filters: Any) -> int:
        stmt = select(func.count()).select_from(AuditLog).filter_by(**filters)
        return int((await self._s.execute(stmt)).scalar_one())
