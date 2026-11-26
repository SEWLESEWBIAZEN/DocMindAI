""" "Continue with Google" sign-in and sign-up (ADR 0040)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Query, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from documind.api.dependencies import (
    AuthServiceDep,
    ContainerDep,
    MetaDep,
    SessionDep,
)
from documind.api.routes._identity_common import set_refresh_cookie
from documind.api.routes.sso import _sso
from documind.application.identity.google_signin import (
    CALLBACK_PATH,
    NONCE_COOKIE,
    STATE_TTL_SECONDS,
    GoogleSignInService,
)
from documind.domain.errors import DomainError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/google", tags=["auth"])


def get_google(
    session: SessionDep, container: ContainerDep, auth: AuthServiceDep, meta: MetaDep
) -> GoogleSignInService:
    return GoogleSignInService(
        session,
        container.settings,
        container.oidc,
        container.redis,
        container.rate_limiter,
        auth,
        _sso(session, container, auth, meta),
        meta,
    )


GoogleDep = Annotated[GoogleSignInService, Depends(get_google)]


class GoogleStatusResponse(BaseModel):
    enabled: bool


class GoogleStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    next: str | None = Field(default=None, max_length=500, description="Where to go afterwards.")


class GoogleStartResponse(BaseModel):
    url: str


@router.get("", response_model=GoogleStatusResponse, summary="Is “Continue with Google” on?")
async def google_status(container: ContainerDep) -> GoogleStatusResponse:
    return GoogleStatusResponse(enabled=container.settings.google_signin_available)


@router.post(
    "/start",
    response_model=GoogleStartResponse,
    summary="Start signing in (or up) with Google",
    description=(
        "Returns Google's sign-in page. Afterwards Google sends the browser to the callback, "
        "which starts a session (or the 2FA step) and continues in the app."
    ),
)
async def google_start(
    body: GoogleStartRequest, response: Response, google: GoogleDep, container: ContainerDep
) -> GoogleStartResponse:
    begun = await google.start(body.next)
    response.set_cookie(
        NONCE_COOKIE,
        begun.nonce,
        max_age=STATE_TTL_SECONDS,
        path=CALLBACK_PATH,
        httponly=True,
        secure=container.settings.auth_cookie_secure,
        samesite="lax",
    )
    return GoogleStartResponse(url=begun.url)


@router.get("/callback", include_in_schema=False)
async def google_callback(
    google: GoogleDep,
    container: ContainerDep,
    state: Annotated[str | None, Query(max_length=200)] = None,
    code: Annotated[str | None, Query(max_length=4000)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
    nonce: Annotated[str | None, Cookie(alias=NONCE_COOKIE)] = None,
) -> RedirectResponse:
    try:
        done = await google.finish(state=state, code=code, error=error, browser=nonce)
        path, session = done.path, done.session
    except DomainError as exc:  # e.g. too many attempts: show it on the sign-in page
        logger.info("google sign-in refused", extra={"code": exc.code})
        path, session = f"/login?google_error={exc.code}", None
    target = f"{container.settings.public_app_url.rstrip('/')}{path}"
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(NONCE_COOKIE, path=CALLBACK_PATH)
    if session is not None:
        set_refresh_cookie(response, container.settings, session)
    return response
