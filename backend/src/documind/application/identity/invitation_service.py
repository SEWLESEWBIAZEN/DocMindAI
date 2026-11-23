"""Invitations: invite by email, accept or decline, revoke."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity import emails
from documind.application.identity.common import AuditRecorder, RequestMeta, utcnow
from documind.application.ports import EmailSender
from documind.core.config import Settings
from documind.domain.errors import (
    ConflictError,
    InvalidTokenError,
    NotFoundError,
    PermissionDeniedError,
)
from documind.domain.identity.permissions import Permission, RoleKey
from documind.domain.identity.policies import (
    WorkspaceContext,
    ensure_can_assign_role,
    ensure_shared_workspace,
)
from documind.domain.identity.tokens import generate_token, hash_token
from documind.domain.identity.values import normalize_email
from documind.infrastructure.database.models import Invitation, User, WorkspaceMember
from documind.infrastructure.database.repositories.identity import (
    AuditLogRepository,
    InvitationRepository,
    RoleRepository,
    UserRepository,
    WorkspaceRepository,
)


@dataclass(frozen=True, slots=True)
class InvitationInfo:
    id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_name: str
    email: str
    role: RoleKey
    status: str
    created_at: datetime
    expires_at: datetime


def _info(invitation: Invitation) -> InvitationInfo:
    return InvitationInfo(
        id=invitation.id,
        workspace_id=invitation.workspace_id,
        workspace_name=invitation.workspace.name,
        email=invitation.email,
        role=RoleKey(invitation.role.key),
        status=invitation.status,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
    )


class InvitationService:
    def __init__(
        self, session: AsyncSession, settings: Settings, email: EmailSender, meta: RequestMeta
    ) -> None:
        self._session = session
        self._settings = settings
        self._email = email
        self._users = UserRepository(session)
        self._roles = RoleRepository(session)
        self._workspaces = WorkspaceRepository(session)
        self._invitations = InvitationRepository(session)
        self._audit = AuditRecorder(AuditLogRepository(session), meta)

    async def invite(
        self, ctx: WorkspaceContext, inviter: User, email: str, role: RoleKey
    ) -> InvitationInfo:
        ensure_shared_workspace(ctx.workspace_kind)
        ctx.require(Permission.MEMBERS_INVITE)
        ensure_can_assign_role(ctx.role, role)
        email = normalize_email(email)
        if await self._workspaces.member_by_email(ctx.workspace_id, email) is not None:
            raise ConflictError(
                "That person is already a member of this workspace.", code="already_member"
            )

        now = utcnow()
        previous = await self._invitations.pending_for_workspace_email(ctx.workspace_id, email)
        if previous is not None:  # re-inviting replaces the old link
            previous.status = "revoked"
            previous.responded_at = now
            await self._session.flush()

        raw = generate_token()
        role_row = await self._roles.get_by_key(role)
        invitation = Invitation(
            id=uuid.uuid4(),
            workspace_id=ctx.workspace_id,
            email=email,
            role_id=role_row.id,
            token_hash=hash_token(raw),
            invited_by_user_id=inviter.id,
            expires_at=now + timedelta(days=self._settings.invitation_ttl_days),
        )
        self._invitations.add(invitation)
        self._audit.record(
            "invitation.created",
            actor_user_id=inviter.id,
            workspace_id=ctx.workspace_id,
            target_type="invitation",
            target_id=invitation.id,
            details={"email": email, "role": role.value, "replaced": previous is not None},
        )
        await self._session.commit()
        stored = await self._invitations.get(ctx.workspace_id, invitation.id)
        if stored is None:  # pragma: no cover - just committed in this transaction
            raise NotFoundError("Invitation not found.")
        await self._email.send(
            emails.invitation_email(
                email,
                inviter.display_name,
                stored.workspace.name,
                role.value,
                self._settings.public_app_url,
                raw,
                self._settings.invitation_ttl_days,
            )
        )
        return _info(stored)

    async def list_pending(self, ctx: WorkspaceContext) -> list[InvitationInfo]:
        ctx.require(Permission.MEMBERS_INVITE)
        return [
            _info(i) for i in await self._invitations.list_pending_for_workspace(ctx.workspace_id)
        ]

    async def revoke(self, ctx: WorkspaceContext, invitation_id: uuid.UUID) -> None:
        ctx.require(Permission.MEMBERS_INVITE)
        invitation = await self._invitations.get(ctx.workspace_id, invitation_id, for_update=True)
        if invitation is None or invitation.status != "pending":
            raise NotFoundError("Invitation not found.")
        invitation.status = "revoked"
        invitation.responded_at = utcnow()
        self._audit.record(
            "invitation.revoked",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="invitation",
            target_id=invitation.id,
            details={"email": invitation.email},
        )
        await self._session.commit()

    # -------------------------------------------------------------- invitee side

    async def list_for_user(self, user: User) -> list[InvitationInfo]:
        if user.email_verified_at is None:
            return []
        return [
            _info(i) for i in await self._invitations.list_pending_for_email(user.email, utcnow())
        ]

    async def accept_by_token(self, user: User, token: str) -> uuid.UUID:
        invitation = await self._invitations.get_by_token_hash(hash_token(token))
        if invitation is None:
            raise InvalidTokenError("This invitation link is invalid or has expired.")
        if invitation.email != user.email:
            raise PermissionDeniedError(
                "This invitation was sent to a different email address.",
                code="invitation_email_mismatch",
            )
        return await self._accept(user, invitation)

    async def accept_by_id(self, user: User, invitation_id: uuid.UUID) -> uuid.UUID:
        invitation = await self._invitations.get_for_email(
            invitation_id, user.email, for_update=True
        )
        if invitation is None:
            raise NotFoundError("Invitation not found.")
        return await self._accept(user, invitation)

    async def decline_by_id(self, user: User, invitation_id: uuid.UUID) -> None:
        invitation = await self._invitations.get_for_email(
            invitation_id, user.email, for_update=True
        )
        if invitation is None or invitation.status != "pending":
            raise NotFoundError("Invitation not found.")
        invitation.status = "declined"
        invitation.responded_at = utcnow()
        self._audit.record(
            "invitation.declined",
            actor_user_id=user.id,
            workspace_id=invitation.workspace_id,
            target_type="invitation",
            target_id=invitation.id,
        )
        await self._session.commit()

    async def _accept(self, user: User, invitation: Invitation) -> uuid.UUID:
        now = utcnow()
        if user.email_verified_at is None:
            raise PermissionDeniedError("Verify your email address before accepting invitations.")
        if invitation.status != "pending" or invitation.expires_at <= now:
            raise InvalidTokenError("This invitation is no longer valid.")
        if not await self._workspaces.is_member(invitation.workspace_id, user.id):
            self._workspaces.add(
                WorkspaceMember(
                    workspace_id=invitation.workspace_id,
                    user_id=user.id,
                    role_id=invitation.role_id,
                )
            )
        invitation.status = "accepted"
        invitation.accepted_by_user_id = user.id
        invitation.responded_at = now
        self._audit.record(
            "invitation.accepted",
            actor_user_id=user.id,
            workspace_id=invitation.workspace_id,
            target_type="invitation",
            target_id=invitation.id,
            details={"role": invitation.role.key},
        )
        await self._session.commit()
        return invitation.workspace_id
