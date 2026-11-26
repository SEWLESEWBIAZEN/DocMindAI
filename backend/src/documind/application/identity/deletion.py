"""Deleting a workspace, an organization or a whole account (ADR 0039).

Order matters, so nothing is left half-done:

1. Refuse what can't be undone safely: custom infrastructure (the customer's own
   database or bucket; move back to DocuMind's first), or an organization that
   would be left without an owner.
2. Stop billing at Stripe. If that fails, nothing is deleted.
3. Delete the rows in one transaction. Foreign keys cascade from the workspace
   (documents, conversations, collections, keys, connectors...) and from the user
   (sessions, tokens, passkeys, memberships, private conversations).
4. After the commit, delete the stored files. A failed object delete leaves an
   orphan in the private bucket (logged), never a row pointing at nothing.

Audit entries stay: they record that the deletion happened, by whom and when.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.billing.gateway import BillingGateway, BillingGatewayError
from documind.application.billing.subscriptions import LIVE
from documind.application.identity import emails
from documind.application.identity.common import AuditRecorder, Principal, RequestMeta, utcnow
from documind.application.infrastructure.resolver import (
    InfrastructureResolver,
    ResolvedInfrastructure,
)
from documind.application.ports import (
    EmailMessage,
    EmailSender,
    ObjectStorageProvider,
    PasswordHasher,
    RateLimiter,
)
from documind.core.config import Settings
from documind.domain.errors import (
    AuthenticationError,
    ConflictError,
    PermissionDeniedError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from documind.domain.identity.policies import WorkspaceContext, ensure_recent_mfa
from documind.infrastructure.database.models import (
    BillingSubscription,
    CollectionMember,
    Conversation,
    DocumentVersion,
    MessageFeedback,
    Organization,
    Role,
    User,
    Workspace,
    WorkspaceMember,
)
from documind.infrastructure.database.repositories.identity import AuditLogRepository

logger = logging.getLogger(__name__)

#: Typed by the person deleting their account, as a last check.
ACCOUNT_CONFIRMATION = "DELETE"


@dataclass(slots=True)
class Purge:
    """Stored files to delete once the rows are gone."""

    jobs: list[tuple[ObjectStorageProvider, list[str]]] = field(default_factory=list)
    emails: list[EmailMessage] = field(default_factory=list)

    async def run(self, sender: EmailSender | None = None) -> int:
        failed = 0
        for storage, keys in self.jobs:
            for key in keys:
                try:
                    await storage.delete(key)
                except Exception:
                    failed += 1
                    logger.exception("orphaned object after deletion", extra={"key": key})
        if sender is not None:
            for message in self.emails:
                try:
                    await sender.send(message)
                except Exception:
                    logger.exception("could not send a deletion notice")
        return failed


def mfa_enabled(user: User) -> bool:
    return bool(user.totp_enabled_at or user.mfa_email_enabled_at or user.mfa_passkey_enabled_at)


def ensure_recent_account_mfa(principal: Principal, now: datetime, max_age_seconds: int) -> None:
    """Account actions: people with 2FA confirm it again; people without use their password."""
    if not mfa_enabled(principal.user):
        return
    verified = principal.mfa_verified_at
    age = (now - verified).total_seconds() if verified else None
    if age is None or age > max_age_seconds or age < -60:
        raise PermissionDeniedError(
            "Confirm it's you with your two-factor code to continue.", code="mfa_step_up_required"
        )


def _custom(infra: ResolvedInfrastructure) -> bool:
    return infra.config.database.mode != "shared" or infra.config.storage.provider != "documind"


class WorkspaceDeleter:
    """Deletes one workspace on DocuMind's shared infrastructure (caller commits)."""

    def __init__(
        self,
        session: AsyncSession,
        infrastructure: InfrastructureResolver,
        gateway: BillingGateway | None,
        audit: AuditRecorder,
    ) -> None:
        self._s = session
        self._infra = infrastructure
        self._gateway = gateway
        self._audit = audit

    async def check(self, workspace: Workspace) -> ResolvedInfrastructure:
        infra = await self._infra.resolve(workspace.id)
        if _custom(infra):
            raise ConflictError(
                f"{workspace.name} uses its own database or storage. Move it back to DocuMind's "
                "infrastructure on the Infrastructure page first, or contact support.",
                code="custom_infrastructure",
            )
        return infra

    async def stop_billing(self, workspace_id: uuid.UUID) -> None:
        row = (
            await self._s.execute(
                select(BillingSubscription)
                .where(BillingSubscription.workspace_id == workspace_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return
        if self._gateway is None:
            if row.subscription_id and row.status in LIVE:
                raise ServiceUnavailableError(
                    "Billing is unavailable, so the subscription can't be cancelled. Try again "
                    "later.",
                    code="billing_unavailable",
                )
            return
        try:
            # Ask Stripe, not just our row: a webhook may not have arrived yet (ADR 0032).
            live = [
                s.id
                for s in await self._gateway.customer_subscriptions(row.customer_id)
                if s.status in LIVE
            ]
            if row.subscription_id and row.status in LIVE and row.subscription_id not in live:
                live.append(row.subscription_id)
            for subscription_id in live:
                await self._gateway.cancel_subscription(subscription_id)
            await self._gateway.expire_open_checkouts(row.customer_id)
        except BillingGatewayError as exc:
            raise ServiceUnavailableError(
                "We couldn't cancel the subscription at Stripe, so nothing was deleted. Try "
                "again in a few minutes.",
                code="billing_unavailable",
            ) from exc

    async def delete(
        self, workspace: Workspace, infra: ResolvedInfrastructure, *, actor_user_id: uuid.UUID
    ) -> tuple[ObjectStorageProvider, list[str]]:
        """Delete the rows; returns the storage and keys to purge after the commit."""
        keys = list(
            (
                await self._s.execute(
                    select(DocumentVersion.storage_key).where(
                        DocumentVersion.workspace_id == workspace.id
                    )
                )
            ).scalars()
        )
        self._audit.record(
            "workspace.deleted",
            actor_user_id=actor_user_id,
            workspace_id=workspace.id,
            target_type="workspace",
            target_id=workspace.id,
            details={"kind": workspace.kind, "name": workspace.name, "files": len(keys)},
        )
        if workspace.organization_id is not None:
            await self._s.execute(
                delete(Organization).where(Organization.id == workspace.organization_id)
            )
        else:
            await self._s.execute(delete(Workspace).where(Workspace.id == workspace.id))
        return infra.storage, keys


async def _memberships(session: AsyncSession, user_id: uuid.UUID) -> list[tuple[Workspace, str]]:
    rows = await session.execute(
        select(Workspace, Role.key)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .join(Role, Role.id == WorkspaceMember.role_id)
        .where(WorkspaceMember.user_id == user_id)
    )
    return [(w, str(role)) for w, role in rows.all()]


async def _owner_count(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    rows = await session.execute(
        select(WorkspaceMember.id)
        .join(Role, Role.id == WorkspaceMember.role_id)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            Role.key == "owner",
            User.is_active.is_(True),
        )
    )
    return len(rows.all())


class AccountDeletionService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        hasher: PasswordHasher,
        limiter: RateLimiter,
        infrastructure: InfrastructureResolver,
        gateway: BillingGateway | None,
        meta: RequestMeta,
        invalidate: Callable[[uuid.UUID], None] = lambda _: None,
    ) -> None:
        self._s = session
        self._settings = settings
        self._hasher = hasher
        self._limiter = limiter
        self._infra = infrastructure
        self._audit = AuditRecorder(AuditLogRepository(session), meta)
        self._deleter = WorkspaceDeleter(session, infrastructure, gateway, self._audit)
        self._invalidate = invalidate

    async def delete_account(self, principal: Principal, password: str, confirm: str) -> Purge:
        """Delete the caller's account, personal workspace and private conversations."""
        s = self._settings
        user = principal.user
        await self._limiter.hit(
            "account-delete:user",
            str(user.id),
            s.rate_limit_login_per_email,
            s.rate_limit_login_window_seconds,
        )
        if confirm.strip() != ACCOUNT_CONFIRMATION:
            raise ValidationFailedError(f'Type "{ACCOUNT_CONFIRMATION}" to confirm.')
        locked = (
            await self._s.execute(select(User).where(User.id == user.id).with_for_update())
        ).scalar_one()
        if not await self._hasher.verify(locked.password_hash, password):
            raise AuthenticationError("Your password is incorrect.", code="invalid_credentials")
        ensure_recent_account_mfa(principal, utcnow(), s.mfa_step_up_ttl_seconds)

        memberships = await _memberships(self._s, locked.id)
        stranded = [
            w.name
            for w, role in memberships
            if w.kind == "organization"
            and role == "owner"
            and await _owner_count(self._s, w.id) < 2
        ]
        if stranded:
            raise ConflictError(
                "You're the only owner of "
                + ", ".join(sorted(stranded))
                + ". Make someone else an owner, or delete the organization, first.",
                code="last_owner",
            )

        purge = Purge()
        personal = next((w for w, _ in memberships if w.kind == "personal"), None)
        personal_infra = await self._deleter.check(personal) if personal is not None else None
        # Organizations on their own database: remove this person's private data there
        # (the shared database does it through foreign keys).
        for w, _ in memberships:
            if w.kind != "organization":
                continue
            infra = await self._infra.resolve(w.id)
            if infra.isolated:
                await _forget_in_data_plane(infra, w.id, locked.id)
        if personal is not None and personal_infra is not None:
            await self._deleter.stop_billing(personal.id)
            purge.jobs.append(
                await self._deleter.delete(personal, personal_infra, actor_user_id=locked.id)
            )
        self._audit.record(
            "user.deleted",
            actor_user_id=locked.id,
            target_type="user",
            target_id=locked.id,
            details={"organizations_left": len(memberships) - (1 if personal else 0)},
        )
        purge.emails.append(emails.account_deleted_email(locked.email, locked.display_name))
        await self._s.execute(delete(User).where(User.id == locked.id))
        await self._s.commit()
        if personal is not None:
            self._invalidate(personal.id)
        return purge

    async def delete_organization(
        self, ctx: WorkspaceContext, actor: User, confirm_name: str
    ) -> Purge:
        """Owners delete an organization, with a recent 2FA check and its name typed."""
        if ctx.workspace_kind != "organization":
            raise ValidationFailedError(
                "A personal workspace is deleted with your account (Profile → Delete account).",
                code="personal_workspace",
            )
        if ctx.role != "owner":
            raise PermissionDeniedError("Only owners can delete an organization.")
        ensure_recent_mfa(ctx, utcnow(), self._settings.mfa_step_up_ttl_seconds)
        workspace = (
            await self._s.execute(
                select(Workspace).where(Workspace.id == ctx.workspace_id).with_for_update()
            )
        ).scalar_one()
        if confirm_name.strip() != workspace.name:
            raise ValidationFailedError("Type the organization's name exactly to confirm.")
        infra = await self._deleter.check(workspace)
        members = (
            await self._s.execute(
                select(User)
                .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
                .where(
                    WorkspaceMember.workspace_id == workspace.id,
                    User.id != actor.id,
                    User.is_active.is_(True),
                )
            )
        ).scalars()
        notices = [
            emails.organization_deleted_email(
                m.email, m.display_name, workspace.name, actor.display_name
            )
            for m in members
        ]
        await self._deleter.stop_billing(workspace.id)
        purge = Purge(emails=notices)
        purge.jobs.append(await self._deleter.delete(workspace, infra, actor_user_id=actor.id))
        await self._s.commit()
        self._invalidate(workspace.id)
        return purge


async def _forget_in_data_plane(
    infra: ResolvedInfrastructure, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    async with infra.data_plane.sessions() as data:
        await data.execute(
            delete(Conversation).where(
                Conversation.workspace_id == workspace_id,
                Conversation.created_by_user_id == user_id,
            )
        )
        await data.execute(
            delete(MessageFeedback).where(
                MessageFeedback.workspace_id == workspace_id, MessageFeedback.user_id == user_id
            )
        )
        await data.execute(
            delete(CollectionMember).where(
                CollectionMember.workspace_id == workspace_id, CollectionMember.user_id == user_id
            )
        )
        await data.commit()
