"""API keys, the MCP server and the Slack app (ADR 0037)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Cookie, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from documind.api.dependencies import (
    CollectionServiceDep,
    ContainerDep,
    DocumentServiceDep,
    MetaDep,
    ProcessingQueriesDep,
    QuotasDep,
    RAGOrchestratorDep,
    RetrievalServiceDep,
    SessionDep,
    WorkspaceCtx,
)
from documind.application.integrations.api_keys import ApiKeyService, plan_allows_integrations
from documind.application.integrations.mcp import (
    PARSE_ERROR,
    PROTOCOL_VERSIONS,
    McpServer,
    WorkspaceTools,
)
from documind.application.integrations.slack import (
    CALLBACK_PATH,
    NONCE_COOKIE,
    STATE_TTL_SECONDS,
    SlackService,
)
from documind.container import Container
from documind.domain.errors import ValidationFailedError
from documind.domain.identity.permissions import Permission
from documind.domain.integrations.api_keys import SCOPE_DESCRIPTIONS, Scope
from documind.infrastructure.database.models import ApiKey

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["integrations"])
slack_router = APIRouter(prefix="/integrations/slack", tags=["integrations"])


def _slack(session: Any, container: Container, meta: Any) -> SlackService:
    return SlackService(
        session,
        container.settings,
        container.slack_client,
        container.redis,
        container.cipher,
        container.rate_limiter,
        container.slack_dispatch,
        meta,
    )


def get_slack(session: SessionDep, container: ContainerDep, meta: MetaDep) -> SlackService:
    return _slack(session, container, meta)


SlackDep = Annotated[SlackService, Depends(get_slack)]


def get_api_keys(session: SessionDep, container: ContainerDep, meta: MetaDep) -> ApiKeyService:
    return ApiKeyService(session, container.settings, meta)


ApiKeysDep = Annotated[ApiKeyService, Depends(get_api_keys)]


# ----------------------------------------------------------------- API keys


class ScopeInfo(BaseModel):
    key: Scope
    description: str


class ApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    scopes: list[Scope] = Field(min_length=1, max_length=3)
    expires_in_days: int | None = Field(default=90, ge=1, le=366)


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_by_user_id: uuid.UUID
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None


class NewApiKeyResponse(ApiKeyResponse):
    token: str = Field(description="Shown once. Send it as `Authorization: Bearer <token>`.")


def _key(row: ApiKey) -> ApiKeyResponse:
    return ApiKeyResponse(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=row.scopes,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
    )


@router.get("/api-keys", response_model=list[ApiKeyResponse], summary="API keys (admins)")
async def list_keys(ctx: WorkspaceCtx, keys: ApiKeysDep) -> list[ApiKeyResponse]:
    return [_key(r) for r in await keys.active(ctx)]


@router.post(
    "/api-keys",
    response_model=NewApiKeyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key (admins; needs a recent 2FA check)",
)
async def create_key(body: ApiKeyRequest, ctx: WorkspaceCtx, keys: ApiKeysDep) -> NewApiKeyResponse:
    row, token = await keys.create(
        ctx,
        name=body.name,
        scopes=[s.value for s in body.scopes],
        expires_in_days=body.expires_in_days,
    )
    return NewApiKeyResponse(**_key(row).model_dump(), token=token)


@router.delete(
    "/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke an API key"
)
async def revoke_key(key_id: uuid.UUID, ctx: WorkspaceCtx, keys: ApiKeysDep) -> Response:
    await keys.revoke(ctx, key_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------- overview


class SlackStatus(BaseModel):
    available: bool = Field(description="Whether this server has a Slack app configured")
    connected: bool
    team_name: str | None
    connected_at: datetime | None
    linked: bool = Field(description="Whether you have linked your Slack account")


class IntegrationsResponse(BaseModel):
    included: bool = Field(description="Whether the workspace's plan includes integrations")
    can_manage: bool
    api_base_url: str
    mcp_url: str
    scopes: list[ScopeInfo]
    slack: SlackStatus


@router.get("/integrations", response_model=IntegrationsResponse, summary="Integrations")
async def overview(
    ctx: WorkspaceCtx, session: SessionDep, slack: SlackDep, container: ContainerDep
) -> IntegrationsResponse:
    base = container.settings.public_app_url.rstrip("/")
    row = await slack.installation(ctx.workspace_id)
    return IntegrationsResponse(
        included=await plan_allows_integrations(session, ctx.workspace_id),
        can_manage=ctx.can(Permission.WORKSPACE_UPDATE),
        api_base_url=f"{base}/api/v1/workspaces/{ctx.workspace_id}",
        mcp_url=f"{base}/api/v1/workspaces/{ctx.workspace_id}/mcp",
        scopes=[ScopeInfo(key=k, description=v) for k, v in SCOPE_DESCRIPTIONS.items()],
        slack=SlackStatus(
            available=slack.available(),
            connected=row is not None,
            team_name=row.team_name if row else None,
            connected_at=row.created_at if row else None,
            linked=await slack.linked(ctx),
        ),
    )


# ---------------------------------------------------------------- Slack


class AuthorizeResponse(BaseModel):
    url: str


class LinkCodeResponse(BaseModel):
    code: str
    command: str
    expires_in_seconds: int


@router.post(
    "/integrations/slack/authorize",
    response_model=AuthorizeResponse,
    summary="Start adding the Slack app (admins; needs a recent 2FA check)",
)
async def slack_authorize(
    ctx: WorkspaceCtx, response: Response, slack: SlackDep, container: ContainerDep
) -> AuthorizeResponse:
    auth = await slack.authorize(ctx)
    response.set_cookie(
        NONCE_COOKIE,
        auth.nonce,
        max_age=STATE_TTL_SECONDS,
        path=CALLBACK_PATH,
        httponly=True,
        secure=container.settings.auth_cookie_secure,
        samesite="lax",
    )
    return AuthorizeResponse(url=auth.url)


@router.delete(
    "/integrations/slack",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove the Slack app (admins; needs a recent 2FA check)",
)
async def slack_disconnect(ctx: WorkspaceCtx, slack: SlackDep) -> Response:
    await slack.disconnect(ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/integrations/slack/link-code",
    response_model=LinkCodeResponse,
    summary="A code to link your Slack account",
)
async def slack_link_code(ctx: WorkspaceCtx, slack: SlackDep) -> LinkCodeResponse:
    from documind.application.integrations.slack import LINK_TTL_SECONDS

    code = await slack.link_code(ctx)
    return LinkCodeResponse(
        code=code, command=f"/documind link {code}", expires_in_seconds=LINK_TTL_SECONDS
    )


@router.delete(
    "/integrations/slack/link",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unlink your Slack account",
)
async def slack_unlink(ctx: WorkspaceCtx, slack: SlackDep) -> Response:
    await slack.unlink_me(ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@slack_router.get("/callback", include_in_schema=False)
async def slack_callback(
    slack: SlackDep,
    container: ContainerDep,
    state: Annotated[str | None, Query(max_length=200)] = None,
    code: Annotated[str | None, Query(max_length=500)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
    nonce: Annotated[str | None, Cookie(alias=NONCE_COOKIE)] = None,
) -> RedirectResponse:
    path = await slack.callback(state=state, code=code, error=error, nonce=nonce)
    target = f"{container.settings.public_app_url.rstrip('/')}{path}"
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(NONCE_COOKIE, path=CALLBACK_PATH)
    return response


_MAX_SLACK_BODY = 64 * 1024


async def _signed_body(
    request: Request, slack: SlackService, timestamp: str | None, signature: str | None
) -> bytes:
    body = await request.body()
    if len(body) > _MAX_SLACK_BODY:
        raise ValidationFailedError("Request too large.", code="payload_too_large")
    slack.verify(timestamp=timestamp, body=body, signature=signature)
    return body


@slack_router.post("/commands", include_in_schema=False)
async def slack_command(
    request: Request,
    slack: SlackDep,
    timestamp: Annotated[str | None, Header(alias="X-Slack-Request-Timestamp")] = None,
    signature: Annotated[str | None, Header(alias="X-Slack-Signature")] = None,
) -> JSONResponse:
    body = await _signed_body(request, slack, timestamp, signature)
    form = dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
    return JSONResponse(await slack.command(form))


@slack_router.post("/events", include_in_schema=False)
async def slack_events(
    request: Request,
    slack: SlackDep,
    timestamp: Annotated[str | None, Header(alias="X-Slack-Request-Timestamp")] = None,
    signature: Annotated[str | None, Header(alias="X-Slack-Signature")] = None,
) -> Response:
    body = await _signed_body(request, slack, timestamp, signature)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise ValidationFailedError("Not JSON.", code="invalid_payload") from exc
    if not isinstance(payload, dict):
        raise ValidationFailedError("Not an event.", code="invalid_payload")
    reply = await slack.event(payload)
    return JSONResponse(reply) if reply is not None else Response(status_code=200)


# ------------------------------------------------------------------ MCP


def _origin_allowed(origin: str | None, container: Container) -> bool:
    """MCP servers must check Origin (DNS rebinding). Non-browser clients send none."""
    if not origin:
        return True
    parts = urlsplit(container.settings.public_app_url)
    own = f"{parts.scheme}://{parts.netloc}"
    return origin == own or origin in container.settings.cors_allowed_origins


@router.post(
    "/mcp",
    summary="MCP server (Streamable HTTP, JSON responses)",
    description=(
        "Model Context Protocol endpoint for AI assistants. Authenticate with an API key "
        "(`Authorization: Bearer dmk_...`). Tools: search, ask, list_documents, "
        "read_document, list_collections."
    ),
    include_in_schema=False,
)
async def mcp(
    request: Request,
    ctx: WorkspaceCtx,
    retrieval: RetrievalServiceDep,
    rag: RAGOrchestratorDep,
    documents: DocumentServiceDep,
    collections: CollectionServiceDep,
    queries: ProcessingQueriesDep,
    quotas: QuotasDep,
    container: ContainerDep,
    origin: Annotated[str | None, Header()] = None,
    protocol: Annotated[str | None, Header(alias="MCP-Protocol-Version")] = None,
) -> Response:
    if not _origin_allowed(origin, container):
        return JSONResponse({"error": "origin not allowed"}, status_code=403)
    if protocol is not None and protocol not in PROTOCOL_VERSIONS:
        return JSONResponse({"error": f"unsupported protocol version {protocol}"}, 400)
    s = container.settings

    async def admit_ai() -> None:
        await container.rate_limiter.hit(
            "ai:user", str(ctx.user_id), s.rate_limit_ai_per_user, s.rate_limit_ai_window_seconds
        )

    server = McpServer(
        WorkspaceTools(
            ctx=ctx,
            retrieval=retrieval,
            rag=rag,
            documents=documents,
            collections=collections,
            queries=queries,
            app_url=s.public_app_url,
            admit_ai=admit_ai,
            admit_answer=quotas.answers,
        )
    )
    raw = await request.body()
    if len(raw) > 256 * 1024:
        return JSONResponse({"error": "request too large"}, status_code=413)
    try:
        message = json.loads(raw)
    except ValueError:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "Bad JSON"}},
            status_code=400,
        )
    if isinstance(message, list):  # JSON-RPC batch (protocol 2025-03-26)
        if not message or len(message) > 20:
            return JSONResponse({"error": "batch must hold 1 to 20 messages"}, 400)
        replies = [r for r in [await server.handle(m) for m in message] if r is not None]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply = await server.handle(message)
    if reply is None:
        return Response(status_code=202)
    return JSONResponse(reply)


@router.get("/mcp", include_in_schema=False)
async def mcp_stream(workspace_id: uuid.UUID) -> Response:
    """No server-initiated messages: there is nothing to stream."""
    del workspace_id
    return Response(status_code=405, headers={"Allow": "POST"})
