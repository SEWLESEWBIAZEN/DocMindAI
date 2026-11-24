"""Tell a workspace's owners and admins that its AI key stopped working (ADR 0019)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.ai.byok import OnInvalid
from documind.application.identity.emails import provider_key_rejected_email
from documind.application.ports import EmailSender
from documind.domain.ai.providers import PROVIDERS
from documind.infrastructure.database.models import Role, User, Workspace, WorkspaceMember


def key_rejected_notifier(
    session_factory: async_sessionmaker[AsyncSession], email: EmailSender, base_url: str
) -> OnInvalid:
    async def notify(workspace_id: uuid.UUID, credential_id: uuid.UUID, provider: str) -> None:
        async with session_factory() as session:
            workspace = await session.get(Workspace, workspace_id)
            admins = (
                await session.execute(
                    select(User.email, User.display_name)
                    .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
                    .join(Role, Role.id == WorkspaceMember.role_id)
                    .where(
                        WorkspaceMember.workspace_id == workspace_id,
                        Role.key.in_(("owner", "admin")),
                    )
                )
            ).all()
        if workspace is None:
            return
        name = PROVIDERS[provider].name if provider in PROVIDERS else provider
        for address, display_name in admins:
            await email.send(
                provider_key_rejected_email(
                    address, display_name, base_url, str(workspace_id), workspace.name, name
                )
            )

    return notify
