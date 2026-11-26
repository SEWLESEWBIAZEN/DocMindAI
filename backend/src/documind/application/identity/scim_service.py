"""SCIM 2.0 user provisioning for organizations with active SSO (ADR 0036).

The identity provider creates, updates and deactivates people; DocuMind keeps
the organization's membership in step. Only emails in the organization's SSO
domains are accepted. Deactivating or deleting removes the person from this
organization only; their account and other workspaces are untouched.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity.common import AuditRecorder, RequestMeta
from documind.application.identity.sso_service import SsoService, domain_of
from documind.domain.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from documind.infrastructure.database.models import Role, SsoConnection, User, WorkspaceMember
from documind.infrastructure.database.repositories.identity import AuditLogRepository

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_FILTER = re.compile(r'^\s*userName\s+eq\s+"([^"]+)"\s*$', re.IGNORECASE)


class ScimService:
    def __init__(self, session: AsyncSession, sso: SsoService, meta: RequestMeta | None = None):
        self._s = session
        self._sso = sso
        self._audit = AuditRecorder(AuditLogRepository(session), meta or RequestMeta())

    async def connection(self, bearer: str | None) -> SsoConnection:
        if not bearer or not bearer.startswith("dmscim_"):
            raise AuthenticationError("Invalid SCIM token.")
        digest = hashlib.sha256(bearer.encode()).hexdigest()
        row = (
            await self._s.execute(
                select(SsoConnection).where(SsoConnection.scim_token_hash == digest)
            )
        ).scalar_one_or_none()
        if (
            row is None
            or row.status != "active"
            or not hmac.compare_digest(digest, row.scim_token_hash or "")
        ):
            raise AuthenticationError("Invalid SCIM token.")
        return row

    @staticmethod
    def resource(user: User, active: bool) -> dict[str, Any]:
        return {
            "schemas": [USER_SCHEMA],
            "id": str(user.id),
            "userName": user.email,
            "displayName": user.display_name,
            "emails": [{"value": user.email, "primary": True}],
            "active": active,
            "meta": {"resourceType": "User"},
        }

    async def _member(self, row: SsoConnection, user_id: uuid.UUID) -> WorkspaceMember | None:
        return (
            await self._s.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == row.workspace_id,
                    WorkspaceMember.user_id == user_id,
                )
            )
        ).scalar_one_or_none()

    async def _user(self, row: SsoConnection, user_id: str) -> User:
        try:
            uid = uuid.UUID(user_id)
        except ValueError as exc:
            raise NotFoundError("User not found.") from exc
        user = await self._s.get(User, uid)
        if user is None or domain_of(user.email) not in row.domains:
            raise NotFoundError("User not found.")
        return user

    async def list(
        self, row: SsoConnection, flt: str | None, start: int, count: int
    ) -> dict[str, Any]:
        stmt = (
            select(User)
            .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
            .where(WorkspaceMember.workspace_id == row.workspace_id)
            .order_by(User.email)
        )
        if flt:
            match = _FILTER.match(flt)
            if not match:
                raise ValidationFailedError("Only 'userName eq \"...\"' filters are supported.")
            stmt = select(User).where(User.email == match.group(1).strip().lower())
        users = [
            u for u in (await self._s.execute(stmt)).scalars() if domain_of(u.email) in row.domains
        ]
        page = users[max(start - 1, 0) : max(start - 1, 0) + count]
        return {
            "schemas": [LIST_SCHEMA],
            "totalResults": len(users),
            "startIndex": start,
            "itemsPerPage": len(page),
            "Resources": [
                self.resource(u, await self._member(row, u.id) is not None) for u in page
            ],
        }

    async def get(self, row: SsoConnection, user_id: str) -> dict[str, Any]:
        user = await self._user(row, user_id)
        return self.resource(user, await self._member(row, user.id) is not None)

    async def create(self, row: SsoConnection, body: dict[str, Any]) -> dict[str, Any]:
        email = str(body.get("userName") or "").strip().lower()
        if "@" not in email or domain_of(email) not in row.domains:
            raise ValidationFailedError("userName must be an email in the organization's domains.")
        name = body.get("displayName") or " ".join(
            str(v)
            for v in (
                (body.get("name") or {}).get("givenName"),
                (body.get("name") or {}).get("familyName"),
            )
            if v
        )
        user = await self._sso.provision(row, email, str(name) if name else None)
        if body.get("active") is False:
            await self._deactivate(row, user)
        return self.resource(user, await self._member(row, user.id) is not None)

    async def update(
        self, row: SsoConnection, user_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """PUT or PATCH. Handles ``active`` and ``displayName``."""
        user = await self._user(row, user_id)
        changes: dict[str, Any] = {}
        if "Operations" in body:  # PATCH
            for op in body.get("Operations") or []:
                path = str(op.get("path") or "")
                value = op.get("value")
                if isinstance(value, dict) and not path:
                    changes.update(value)
                elif path:
                    changes[path] = value
        else:
            changes = body
        if isinstance(changes.get("displayName"), str) and changes["displayName"].strip():
            user.display_name = changes["displayName"].strip()[:100]
        active = changes.get("active")
        if isinstance(active, str):
            active = active.lower() == "true"
        if active is False:
            await self._deactivate(row, user)
        elif active is True and await self._member(row, user.id) is None:
            await self._sso.provision(row, user.email, None)
        await self._s.commit()
        return self.resource(user, await self._member(row, user.id) is not None)

    async def delete(self, row: SsoConnection, user_id: str) -> None:
        user = await self._user(row, user_id)
        await self._deactivate(row, user)
        await self._s.commit()

    async def _deactivate(self, row: SsoConnection, user: User) -> None:
        member = await self._member(row, user.id)
        if member is not None:
            owner = (await self._s.execute(select(Role.id).where(Role.key == "owner"))).scalar_one()
            if member.role_id == owner:
                owners = (
                    await self._s.execute(
                        select(func.count())
                        .select_from(WorkspaceMember)
                        .where(
                            WorkspaceMember.workspace_id == row.workspace_id,
                            WorkspaceMember.role_id == owner,
                        )
                    )
                ).scalar_one()
                if owners <= 1:
                    raise ConflictError("The organization's last owner can't be removed.")
        await self._s.execute(
            delete(WorkspaceMember).where(
                WorkspaceMember.workspace_id == row.workspace_id, WorkspaceMember.user_id == user.id
            )
        )
        if user.default_workspace_id == row.workspace_id:
            user.default_workspace_id = None
        self._audit.record(
            "member.deprovisioned",
            actor_user_id=None,
            workspace_id=row.workspace_id,
            target_type="user",
            target_id=user.id,
            details={"via": "scim"},
        )
        await self._s.commit()
