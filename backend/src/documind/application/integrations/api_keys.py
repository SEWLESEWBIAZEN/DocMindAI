"""Workspace API keys (ADR 0037).

Keys are shown once and stored as SHA-256 hashes: they are 256-bit random
tokens, so a fast hash is enough. A key acts as the admin who created it,
limited by its scopes, and stops when they leave the workspace, when it is
revoked or expires, or when the workspace drops below Pro.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity.common import (
    ApiKeyGrant,
    AuditRecorder,
    Principal,
    RequestMeta,
    utcnow,
)
from documind.core.config import Settings
from documind.domain.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext, ensure_recent_mfa
from documind.domain.integrations.api_keys import (
    INTEGRATION_PLANS,
    MAX_KEYS_PER_WORKSPACE,
    TOKEN_PREFIX,
    Scope,
)
from documind.infrastructure.database.models import ApiKey, User, WorkspacePlan
from documind.infrastructure.database.repositories.identity import AuditLogRepository

_INVALID = "This API key isn't valid. Create a new one in the workspace's integrations."
_TOUCH_EVERY = timedelta(minutes=1)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def plan_allows_integrations(session: AsyncSession, workspace_id: uuid.UUID) -> bool:
    plan = await session.get(WorkspacePlan, workspace_id)
    return plan is not None and plan.plan_key in INTEGRATION_PLANS


async def require_integrations_plan(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    if not await plan_allows_integrations(session, workspace_id):
        raise PermissionDeniedError(
            "API keys, the MCP server and Slack are included from the Pro plan.",
            code="integrations_plan_required",
        )


class ApiKeyService:
    def __init__(self, session: AsyncSession, settings: Settings, meta: RequestMeta) -> None:
        self._s = session
        self._settings = settings
        self._audit = AuditRecorder(AuditLogRepository(session), meta)

    async def active(self, ctx: WorkspaceContext) -> list[ApiKey]:
        ctx.require(Permission.WORKSPACE_UPDATE)
        rows = await self._s.execute(
            select(ApiKey)
            .where(ApiKey.workspace_id == ctx.workspace_id, ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.created_at.desc())
        )
        return list(rows.scalars())

    async def create(
        self,
        ctx: WorkspaceContext,
        *,
        name: str,
        scopes: list[str],
        expires_in_days: int | None,
    ) -> tuple[ApiKey, str]:
        ctx.require(Permission.WORKSPACE_UPDATE)
        ensure_recent_mfa(ctx, utcnow(), self._settings.mfa_step_up_ttl_seconds)
        await require_integrations_plan(self._s, ctx.workspace_id)
        name = " ".join(name.split())
        if not name:
            raise ValidationFailedError("Give the key a name.", code="invalid_name")
        try:
            chosen = sorted({Scope(s).value for s in scopes})
        except ValueError as exc:
            raise ValidationFailedError("Unknown scope.", code="invalid_scopes") from exc
        if not chosen:
            raise ValidationFailedError("Choose at least one scope.", code="invalid_scopes")
        if expires_in_days is not None and not 1 <= expires_in_days <= 366:
            raise ValidationFailedError(
                "Keys expire after 1 to 366 days, or never.", code="invalid_expiry"
            )
        active = await self._s.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.workspace_id == ctx.workspace_id, ApiKey.revoked_at.is_(None))
        )
        if (active or 0) >= MAX_KEYS_PER_WORKSPACE:
            raise ConflictError(
                f"A workspace can have {MAX_KEYS_PER_WORKSPACE} keys. Revoke one first.",
                code="too_many_keys",
            )
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        row = ApiKey(
            id=uuid.uuid4(),
            workspace_id=ctx.workspace_id,
            created_by_user_id=ctx.user_id,
            name=name[:80],
            prefix=token[:12],
            token_hash=digest(token),
            scopes=chosen,
            expires_at=utcnow() + timedelta(days=expires_in_days) if expires_in_days else None,
        )
        self._s.add(row)
        self._audit.record(
            "api_key.created",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="api_key",
            target_id=row.id,
            details={"name": row.name, "scopes": chosen, "expires_at": str(row.expires_at)},
        )
        await self._s.commit()
        await self._s.refresh(row)
        return row, token

    async def revoke(self, ctx: WorkspaceContext, key_id: uuid.UUID) -> None:
        ctx.require(Permission.WORKSPACE_UPDATE)
        row = await self._s.get(ApiKey, key_id)
        if row is None or row.workspace_id != ctx.workspace_id or row.revoked_at is not None:
            raise NotFoundError("API key not found.")
        row.revoked_at = utcnow()
        self._audit.record(
            "api_key.revoked",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="api_key",
            target_id=row.id,
            details={"name": row.name},
        )
        await self._s.commit()

    async def authenticate(self, token: str) -> Principal:
        row = (
            await self._s.execute(select(ApiKey).where(ApiKey.token_hash == digest(token)))
        ).scalar_one_or_none()
        now = utcnow()
        if (
            row is None
            or row.revoked_at is not None
            or (row.expires_at is not None and row.expires_at <= now)
        ):
            raise AuthenticationError(_INVALID, code="invalid_api_key")
        user = await self._s.get(User, row.created_by_user_id)
        if user is None or not user.is_active:
            raise AuthenticationError(_INVALID, code="invalid_api_key")
        await require_integrations_plan(self._s, row.workspace_id)
        if row.last_used_at is None or now - row.last_used_at > _TOUCH_EVERY:
            await self._s.execute(
                update(ApiKey).where(ApiKey.id == row.id).values(last_used_at=now)
            )
            await self._s.commit()
        return Principal(
            user=user,
            api_key=ApiKeyGrant(row.id, row.workspace_id, tuple(row.scopes)),
        )
