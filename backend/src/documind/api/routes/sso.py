"""Organization single sign-on (OIDC) and SCIM provisioning (ADR 0036)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Cookie, Depends, Header, Query, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from documind.api.dependencies import (
    AuthServiceDep,
    ContainerDep,
    MetaDep,
    SessionDep,
    WorkspaceCtx,
)
from documind.api.routes._identity_common import set_refresh_cookie
from documind.application.identity.auth_service import AuthService
from documind.application.identity.scim_service import ScimService
from documind.application.identity.sso_service import (
    CALLBACK_PATH,
    NONCE_COOKIE,
    STATE_TTL_SECONDS,
    SsoService,
)
from documind.container import Container
from documind.domain.errors import DomainError
from documind.infrastructure.database.models import SsoConnection

auth_router = APIRouter(prefix="/auth/sso", tags=["sso"])
router = APIRouter(prefix="/workspaces/{workspace_id}/sso", tags=["sso"])
scim_router = APIRouter(prefix="/scim/v2", tags=["scim"], include_in_schema=False)


def _sso(session: Any, container: Container, auth: AuthService, meta: Any) -> SsoService:
    return SsoService(
        session,
        container.settings,
        container.oidc,
        container.redis,
        container.cipher,
        container.password_hasher,
        auth,
        meta,
    )


def get_sso(
    session: SessionDep, container: ContainerDep, auth: AuthServiceDep, meta: MetaDep
) -> SsoService:
    return _sso(session, container, auth, meta)


SsoDep = Annotated[SsoService, Depends(get_sso)]


class EmailBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr


class DiscoverResponse(BaseModel):
    sso: bool
    required: bool


class StartResponse(BaseModel):
    url: str


class SsoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issuer: str = Field(min_length=8, max_length=500)
    client_id: str = Field(min_length=1, max_length=300)
    client_secret: str | None = Field(default=None, max_length=2000)
    domains: list[str] = Field(min_length=1, max_length=20)
    default_role: Literal["member", "viewer"] = "member"
    enforced: bool = False


class SsoResponse(BaseModel):
    id: uuid.UUID
    issuer: str
    client_id: str
    has_client_secret: bool
    domains: list[str]
    default_role: str
    enforced: bool
    status: Literal["pending_verification", "active", "disabled"]
    scim_enabled: bool
    scim_token_prefix: str | None
    redirect_uri: str
    scim_base_url: str
    updated_at: datetime


class ScimTokenResponse(BaseModel):
    token: str = Field(description="Shown once. Use as the Bearer token in your identity provider.")


def _response(row: SsoConnection, sso: SsoService, base: str) -> SsoResponse:
    return SsoResponse(
        id=row.id,
        issuer=row.issuer,
        client_id=row.client_id,
        has_client_secret=row.client_secret_credential_id is not None,
        domains=row.domains,
        default_role=row.default_role,
        enforced=row.enforced,
        status=row.status,
        scim_enabled=row.scim_token_hash is not None,
        scim_token_prefix=row.scim_token_prefix,
        redirect_uri=sso.redirect_uri(),
        scim_base_url=f"{base.rstrip('/')}/api/v1/scim/v2",
        updated_at=row.updated_at,
    )


# ----------------------------------------------------------------- sign-in


@auth_router.post("/discover", response_model=DiscoverResponse, summary="Does this email use SSO?")
async def discover(body: EmailBody, sso: SsoDep, session: SessionDep) -> DiscoverResponse:
    from documind.application.identity.sso_service import sso_required_for

    row = await sso.for_email(str(body.email))
    required = (
        row is not None and await sso_required_for(session, str(body.email).lower()) is not None
    )
    return DiscoverResponse(sso=row is not None, required=required)


@auth_router.post("/start", response_model=StartResponse, summary="Start signing in with SSO")
async def start(
    body: EmailBody, response: Response, sso: SsoDep, container: ContainerDep
) -> StartResponse:
    begun = await sso.start(str(body.email).lower())
    response.set_cookie(
        NONCE_COOKIE,
        begun.nonce,
        max_age=STATE_TTL_SECONDS,
        path=CALLBACK_PATH,
        httponly=True,
        secure=container.settings.auth_cookie_secure,
        samesite="lax",
    )
    return StartResponse(url=begun.url)


@auth_router.get("/callback", include_in_schema=False)
async def callback(
    sso: SsoDep,
    container: ContainerDep,
    state: Annotated[str | None, Query(max_length=200)] = None,
    code: Annotated[str | None, Query(max_length=4000)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
    nonce: Annotated[str | None, Cookie(alias=NONCE_COOKIE)] = None,
) -> RedirectResponse:
    done = await sso.finish(state=state, code=code, error=error, browser=nonce)
    target = f"{container.settings.public_app_url.rstrip('/')}{done.path}"
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(NONCE_COOKIE, path=CALLBACK_PATH)
    if done.session is not None:
        set_refresh_cookie(response, container.settings, done.session)
    return response


# ------------------------------------------------------------- admin setup


@router.get("", response_model=SsoResponse | None, summary="This organization's SSO settings")
async def get_settings(
    ctx: WorkspaceCtx, sso: SsoDep, container: ContainerDep
) -> SsoResponse | None:
    row = await sso.get(ctx)
    return _response(row, sso, container.settings.public_app_url) if row else None


@router.put("", response_model=SsoResponse, summary="Set up SSO (admins; needs a recent 2FA check)")
async def save_settings(
    body: SsoRequest, ctx: WorkspaceCtx, sso: SsoDep, container: ContainerDep
) -> SsoResponse:
    row = await sso.save(
        ctx,
        issuer=body.issuer,
        client_id=body.client_id,
        client_secret=body.client_secret,
        domains=body.domains,
        default_role=body.default_role,
        enforced=body.enforced,
    )
    return _response(row, sso, container.settings.public_app_url)


@router.post("/scim-token", response_model=ScimTokenResponse, summary="New SCIM token (shown once)")
async def new_scim_token(ctx: WorkspaceCtx, sso: SsoDep) -> ScimTokenResponse:
    return ScimTokenResponse(token=await sso.scim_token(ctx))


# -------------------------------------------------------------------- SCIM

_SCIM_JSON = "application/scim+json"


def _scim(body: Any, code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=code, media_type=_SCIM_JSON)


def _scim_error(exc: DomainError, code: int) -> JSONResponse:
    return _scim(
        {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
            "status": str(code),
            "detail": exc.message,
        },
        code,
    )


async def _scim_service(
    session: SessionDep, container: ContainerDep, auth: AuthServiceDep, meta: MetaDep
) -> ScimService:
    return ScimService(session, _sso(session, container, auth, meta), meta)


ScimDep = Annotated[ScimService, Depends(_scim_service)]


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def _call(scim: ScimService, authorization: str | None, fn: Any, ok: int = 200) -> Response:
    from documind.domain.errors import (
        AuthenticationError,
        ConflictError,
        NotFoundError,
        ValidationFailedError,
    )

    try:
        row = await scim.connection(_bearer(authorization))
        result = await fn(row)
    except AuthenticationError as exc:
        return _scim_error(exc, 401)
    except NotFoundError as exc:
        return _scim_error(exc, 404)
    except ConflictError as exc:
        return _scim_error(exc, 409)
    except ValidationFailedError as exc:
        return _scim_error(exc, 400)
    if result is None:
        return Response(status_code=204)
    return _scim(result, ok)


@scim_router.get("/ServiceProviderConfig")
async def service_provider_config() -> JSONResponse:
    return _scim(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 200},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {"type": "oauthbearertoken", "name": "Bearer token", "description": "SCIM token"}
            ],
        }
    )


@scim_router.get("/Users")
async def scim_list(
    scim: ScimDep,
    authorization: Annotated[str | None, Header()] = None,
    filter: Annotated[str | None, Query(max_length=300)] = None,
    startIndex: Annotated[int, Query(ge=1)] = 1,  # noqa: N803 - SCIM name
    count: Annotated[int, Query(ge=0, le=200)] = 100,
) -> Response:
    return await _call(scim, authorization, lambda row: scim.list(row, filter, startIndex, count))


@scim_router.get("/Users/{user_id}")
async def scim_get(
    user_id: str, scim: ScimDep, authorization: Annotated[str | None, Header()] = None
) -> Response:
    return await _call(scim, authorization, lambda row: scim.get(row, user_id))


@scim_router.post("/Users")
async def scim_create(
    scim: ScimDep,
    body: Annotated[dict[str, Any], Body()],
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    return await _call(scim, authorization, lambda row: scim.create(row, body), ok=201)


@scim_router.put("/Users/{user_id}")
@scim_router.patch("/Users/{user_id}")
async def scim_update(
    user_id: str,
    scim: ScimDep,
    body: Annotated[dict[str, Any], Body()],
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    return await _call(scim, authorization, lambda row: scim.update(row, user_id, body))


@scim_router.delete("/Users/{user_id}")
async def scim_delete(
    user_id: str, scim: ScimDep, authorization: Annotated[str | None, Header()] = None
) -> Response:
    async def go(row: SsoConnection) -> None:
        await scim.delete(row, user_id)

    return await _call(scim, authorization, go)
