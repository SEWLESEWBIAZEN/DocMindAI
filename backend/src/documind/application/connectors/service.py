"""Connecting a drive, choosing folders, syncing on demand, disconnecting (ADR 0035).

The OAuth callback is bound to the browser that started it: besides the
single-use ``state`` (kept in Redis with the PKCE verifier), a random nonce is set
as an HttpOnly cookie at authorization and must come back with the callback.
Without it, someone could send a victim their own authorization link and connect
the victim's Drive to the sender's workspace.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.connectors.ports import ConnectorAuthError, ConnectorError, DriveClient
from documind.application.credentials.vault import CredentialVault
from documind.application.documents.collection_service import CollectionService
from documind.application.identity.common import AuditRecorder, RequestMeta, utcnow
from documind.core.config import Settings
from documind.domain.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext, ensure_recent_mfa
from documind.infrastructure.database.models import (
    ConnectorConnection,
    ConnectorItem,
    WorkspaceMember,
)
from documind.infrastructure.database.repositories.identity import AuditLogRepository
from documind.infrastructure.security.vault import EnvelopeCipher

PROVIDER = "google_drive"
STATE_TTL_SECONDS = 600
NONCE_COOKIE = "documind_oauth_nonce"
MAX_FOLDERS = 20
Dispatch = Callable[[uuid.UUID, uuid.UUID], Awaitable[bool]]


def redirect_uri(settings: Settings) -> str:
    return f"{settings.public_app_url.rstrip('/')}/api/v1/connectors/google-drive/callback"


def _key(state: str) -> str:
    return f"connector:oauth:{hashlib.sha256(state.encode()).hexdigest()}"


def _digest(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Authorization:
    url: str
    nonce: str


class ConnectorService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        drive: DriveClient | None,
        redis: Redis,
        cipher: EnvelopeCipher | None,
        dispatch: Dispatch,
        meta: RequestMeta | None = None,
    ) -> None:
        self._s = session
        self._settings = settings
        self._drive = drive
        self._redis = redis
        self._vault = CredentialVault(session, cipher)
        self._dispatch = dispatch
        self._audit = AuditRecorder(AuditLogRepository(session), meta or RequestMeta())

    # ------------------------------------------------------------ helpers

    def _require_drive(self) -> DriveClient:
        if self._drive is None:
            raise ServiceUnavailableError(
                "Google Drive isn't set up on this server.", code="connector_not_configured"
            )
        return self._drive

    @staticmethod
    def _require_manage(ctx: WorkspaceContext) -> None:
        ctx.require(Permission.WORKSPACE_UPDATE)

    async def _get(self, ctx: WorkspaceContext, connection_id: uuid.UUID) -> ConnectorConnection:
        row = await self._s.get(ConnectorConnection, connection_id)
        if row is None or row.workspace_id != ctx.workspace_id:
            raise NotFoundError("Connection not found.")
        return row

    async def _token(self, row: ConnectorConnection) -> str:
        drive = self._require_drive()
        cred = (
            await self._vault.get(row.workspace_id, row.credential_id)
            if row.credential_id
            else None
        )
        if cred is None:
            raise ConflictError("Connect Google Drive again.", code="connector_reconnect")
        try:
            return await drive.access_token(self._vault.reveal(cred))
        except ConnectorAuthError as exc:
            row.status, row.last_error = "error", "reconnect"
            await self._s.commit()
            raise ConflictError(
                "Google Drive access was revoked or expired. Connect it again.",
                code="connector_reconnect",
            ) from exc
        except ConnectorError as exc:
            raise ServiceUnavailableError(
                "Google Drive is unavailable. Try again shortly.", code="connector_unavailable"
            ) from exc

    # -------------------------------------------------------------- reads

    async def connections(self, ctx: WorkspaceContext) -> list[ConnectorConnection]:
        ctx.require(Permission.WORKSPACE_READ)
        rows = await self._s.execute(
            select(ConnectorConnection)
            .where(ConnectorConnection.workspace_id == ctx.workspace_id)
            .order_by(ConnectorConnection.created_at)
        )
        return list(rows.scalars())

    # --------------------------------------------------------- connecting

    async def authorize(self, ctx: WorkspaceContext) -> Authorization:
        """Start the OAuth flow. Needs a recent 2FA check, like other credential changes."""
        self._require_manage(ctx)
        ensure_recent_mfa(ctx, utcnow(), self._settings.mfa_step_up_ttl_seconds)
        drive = self._require_drive()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        nonce = secrets.token_urlsafe(24)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        await self._redis.set(
            _key(state),
            json.dumps(
                {
                    "workspace_id": str(ctx.workspace_id),
                    "user_id": str(ctx.user_id),
                    "verifier": verifier,
                    "nonce": _digest(nonce),
                }
            ),
            ex=STATE_TTL_SECONDS,
        )
        url = drive.authorize_url(
            state=state, code_challenge=challenge, redirect_uri=redirect_uri(self._settings)
        )
        return Authorization(url, nonce)

    async def callback(
        self, *, state: str | None, code: str | None, error: str | None, nonce: str | None
    ) -> str:
        """Finish the OAuth flow. Returns the app path to send the browser to."""
        raw = await self._redis.getdel(_key(state)) if state else None
        if raw is None:
            return "/app?connector_error=expired"
        pending: dict[str, Any] = json.loads(raw)
        workspace_id = uuid.UUID(pending["workspace_id"])
        user_id = uuid.UUID(pending["user_id"])
        back = f"/w/{workspace_id}/connectors"
        if not nonce or not hmac.compare_digest(_digest(nonce), pending["nonce"]):
            return f"{back}?connector_error=browser"
        if error or not code:
            return f"{back}?connector_error=denied"
        member = (
            await self._s.execute(
                select(WorkspaceMember.id).where(
                    WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if member is None:
            return "/app?connector_error=denied"
        drive = self._require_drive()
        try:
            tokens = await drive.exchange_code(
                code=code,
                code_verifier=pending["verifier"],
                redirect_uri=redirect_uri(self._settings),
            )
        except ConnectorError:
            return f"{back}?connector_error=failed"
        cred = self._vault.store(
            workspace_id, "connector_token", tokens.refresh_token, created_by=user_id
        )
        await self._s.flush()  # the connection references it
        row = ConnectorConnection(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            provider=PROVIDER,
            status="needs_folders",
            account_email=(tokens.account_email or "")[:320] or None,
            credential_id=cred.id,
            folders=[],
            known_folders=[],
            created_by_user_id=user_id,
        )
        self._s.add(row)
        self._audit.record(
            "connector.connected",
            actor_user_id=user_id,
            workspace_id=workspace_id,
            target_type="connector",
            target_id=row.id,
            details={"provider": PROVIDER, "account": row.account_email},
        )
        await self._s.commit()
        return f"{back}?connected={row.id}"

    async def folders(
        self, ctx: WorkspaceContext, connection_id: uuid.UUID, parent: str | None
    ) -> list[dict[str, str]]:
        self._require_manage(ctx)
        row = await self._get(ctx, connection_id)
        token = await self._token(row)
        try:
            found = await self._require_drive().folders(token, parent)
        except ConnectorError as exc:
            raise ServiceUnavailableError(
                "Google Drive is unavailable. Try again shortly.", code="connector_unavailable"
            ) from exc
        return [{"id": f.id, "name": f.name} for f in found]

    async def configure(
        self,
        ctx: WorkspaceContext,
        connection_id: uuid.UUID,
        folders: list[dict[str, str]],
        collections: CollectionService,
        collection_name: str | None = None,
    ) -> ConnectorConnection:
        self._require_manage(ctx)
        row = await self._get(ctx, connection_id)
        cleaned = [
            {"id": str(f["id"])[:200], "name": " ".join(str(f.get("name", "")).split())[:200]}
            for f in folders
            if f.get("id")
        ]
        if not cleaned:
            raise ValidationFailedError("Choose at least one folder.", code="no_folders")
        if len(cleaned) > MAX_FOLDERS:
            raise ValidationFailedError(
                f"Choose at most {MAX_FOLDERS} folders.", code="too_many_folders"
            )
        if row.collection_id is None or not any(
            c.id == row.collection_id for c in await collections.list(ctx)
        ):
            name = " ".join((collection_name or "Google Drive").split())[:100] or "Google Drive"
            existing = next((c for c in await collections.list(ctx) if c.name == name), None)
            collection = existing or await collections.create(
                ctx, name, "Kept in sync with Google Drive."
            )
            row.collection_id = collection.id
        changed = {f["id"] for f in cleaned} != {f["id"] for f in row.folders}
        row.folders = cleaned
        if changed:
            row.cursor, row.known_folders = None, []  # walk the new folders from scratch
        row.status, row.last_error = "active", None
        self._audit.record(
            "connector.configured",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="connector",
            target_id=row.id,
            details={"folders": len(cleaned)},
        )
        await self._s.commit()
        await self._dispatch(row.id, row.workspace_id)
        return row

    async def sync_now(
        self, ctx: WorkspaceContext, connection_id: uuid.UUID
    ) -> ConnectorConnection:
        self._require_manage(ctx)
        row = await self._get(ctx, connection_id)
        if row.status == "needs_folders":
            raise ConflictError("Choose folders first.", code="no_folders")
        if row.last_error == "reconnect":
            raise ConflictError("Connect Google Drive again.", code="connector_reconnect")
        row.status = "active"
        await self._s.commit()
        await self._dispatch(row.id, row.workspace_id)
        return row

    async def disconnect(
        self, ctx: WorkspaceContext, connection_id: uuid.UUID, data: AsyncSession
    ) -> None:
        """Stop syncing. Imported documents stay in the workspace."""
        self._require_manage(ctx)
        ensure_recent_mfa(ctx, utcnow(), self._settings.mfa_step_up_ttl_seconds)
        row = await self._get(ctx, connection_id)
        cred = (
            await self._vault.get(row.workspace_id, row.credential_id)
            if row.credential_id
            else None
        )
        if cred is not None:
            if self._drive is not None and cred.status == "active":
                with contextlib.suppress(Exception):
                    await self._drive.revoke(self._vault.reveal(cred))
            self._vault.revoke(cred)
        await data.execute(
            delete(ConnectorItem).where(
                ConnectorItem.workspace_id == ctx.workspace_id,
                ConnectorItem.connection_id == row.id,
            )
        )
        if data is not self._s:
            await data.commit()
        await self._s.delete(row)
        self._audit.record(
            "connector.disconnected",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="connector",
            target_id=connection_id,
            details={"provider": row.provider},
        )
        await self._s.commit()
