"""Connectors that keep documents in sync with Google Drive (ADR 0035)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Query, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from documind.api.dependencies import (
    CollectionServiceDep,
    ContainerDep,
    DataSessionDep,
    MetaDep,
    SessionDep,
    WorkspaceCtx,
)
from documind.application.connectors.service import (
    NONCE_COOKIE,
    STATE_TTL_SECONDS,
    ConnectorService,
    redirect_uri,
)
from documind.container import Container
from documind.infrastructure.database.models import ConnectorConnection

router = APIRouter(prefix="/workspaces/{workspace_id}/connectors", tags=["connectors"])
public_router = APIRouter(prefix="/connectors", tags=["connectors"])
_CALLBACK_PATH = "/api/v1/connectors/google-drive/callback"


def _service(session: SessionDep, container: Container, meta: MetaDep) -> ConnectorService:
    return ConnectorService(
        session,
        container.settings,
        container.drive_client,
        container.redis,
        container.cipher,
        container.connectors(),
        meta,
    )


class ProviderInfo(BaseModel):
    key: Literal["google_drive"]
    name: str
    available: bool = Field(description="An OAuth client is configured on this server.")
    redirect_uri: str


class ConnectionResponse(BaseModel):
    id: uuid.UUID
    provider: Literal["google_drive"]
    status: Literal["needs_folders", "active", "error", "paused"]
    account_email: str | None
    folders: list[dict[str, str]]
    collection_id: uuid.UUID | None
    files_synced: int
    files_skipped: int
    last_sync_at: datetime | None
    last_error: str | None
    initial_sync_done: bool
    created_at: datetime


class ConnectorsResponse(BaseModel):
    providers: list[ProviderInfo]
    connections: list[ConnectionResponse]


class AuthorizeResponse(BaseModel):
    url: str


class FolderResponse(BaseModel):
    id: str
    name: str


class ConfigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folders: list[FolderResponse] = Field(min_length=1, max_length=20)
    collection_name: str | None = Field(default=None, max_length=100)


def _connection(row: ConnectorConnection) -> ConnectionResponse:
    return ConnectionResponse(
        id=row.id,
        provider="google_drive",
        status=row.status,
        account_email=row.account_email,
        folders=row.folders,
        collection_id=row.collection_id,
        files_synced=row.files_synced,
        files_skipped=row.files_skipped,
        last_sync_at=row.last_sync_at,
        last_error=row.last_error,
        initial_sync_done=row.cursor is not None,
        created_at=row.created_at,
    )


@router.get("", response_model=ConnectorsResponse)
async def list_connectors(
    ctx: WorkspaceCtx, session: SessionDep, container: ContainerDep, meta: MetaDep
) -> ConnectorsResponse:
    service = _service(session, container, meta)
    return ConnectorsResponse(
        providers=[
            ProviderInfo(
                key="google_drive",
                name="Google Drive",
                available=container.drive_client is not None,
                redirect_uri=redirect_uri(container.settings),
            )
        ],
        connections=[_connection(r) for r in await service.connections(ctx)],
    )


@router.post(
    "/google-drive/authorize",
    response_model=AuthorizeResponse,
    summary="Start connecting Google Drive (admins; needs a recent 2FA check)",
)
async def authorize_google_drive(
    ctx: WorkspaceCtx,
    response: Response,
    session: SessionDep,
    container: ContainerDep,
    meta: MetaDep,
) -> AuthorizeResponse:
    auth = await _service(session, container, meta).authorize(ctx)
    # Binds the callback to this browser (see ConnectorService).
    response.set_cookie(
        NONCE_COOKIE,
        auth.nonce,
        max_age=STATE_TTL_SECONDS,
        path=_CALLBACK_PATH,
        httponly=True,
        secure=container.settings.auth_cookie_secure,
        samesite="lax",
    )
    return AuthorizeResponse(url=auth.url)


@public_router.get("/google-drive/callback", include_in_schema=False)
async def google_drive_callback(
    session: SessionDep,
    container: ContainerDep,
    meta: MetaDep,
    state: Annotated[str | None, Query(max_length=200)] = None,
    code: Annotated[str | None, Query(max_length=2000)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
    nonce: Annotated[str | None, Cookie(alias=NONCE_COOKIE)] = None,
) -> RedirectResponse:
    path = await _service(session, container, meta).callback(
        state=state, code=code, error=error, nonce=nonce
    )
    target = f"{container.settings.public_app_url.rstrip('/')}{path}"
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(NONCE_COOKIE, path=_CALLBACK_PATH)
    return response


@router.get("/{connection_id}/folders", response_model=list[FolderResponse])
async def list_folders(
    connection_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    container: ContainerDep,
    meta: MetaDep,
    parent: Annotated[str | None, Query(max_length=200)] = None,
) -> list[FolderResponse]:
    found = await _service(session, container, meta).folders(ctx, connection_id, parent)
    return [FolderResponse(**f) for f in found]


@router.put("/{connection_id}", response_model=ConnectionResponse, summary="Choose folders to sync")
async def configure_connection(
    connection_id: uuid.UUID,
    body: ConfigureRequest,
    ctx: WorkspaceCtx,
    session: SessionDep,
    container: ContainerDep,
    meta: MetaDep,
    collections: CollectionServiceDep,
) -> ConnectionResponse:
    row = await _service(session, container, meta).configure(
        ctx,
        connection_id,
        [f.model_dump() for f in body.folders],
        collections,
        body.collection_name,
    )
    return _connection(row)


@router.post("/{connection_id}/sync", response_model=ConnectionResponse, status_code=202)
async def sync_connection(
    connection_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    container: ContainerDep,
    meta: MetaDep,
) -> ConnectionResponse:
    return _connection(await _service(session, container, meta).sync_now(ctx, connection_id))


@router.delete(
    "/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disconnect (needs a recent 2FA check). Imported documents stay.",
)
async def disconnect(
    connection_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    data: DataSessionDep,
    container: ContainerDep,
    meta: MetaDep,
) -> Response:
    await _service(session, container, meta).disconnect(ctx, connection_id, data)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
