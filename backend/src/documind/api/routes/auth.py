"""Authentication endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse

from documind.api.dependencies import (
    AuthServiceDep,
    ContainerDep,
    CurrentUser,
    MfaServiceDep,
    SettingsDep,
    require_csrf_header,
)
from documind.api.routes._identity_common import clear_refresh_cookie, token_response
from documind.api.schemas.identity import (
    EmailRequest,
    LoginRequest,
    MessageResponse,
    MfaChallengeResponse,
    MfaChallengeTokenRequest,
    MfaLoginRequest,
    MfaSecondFactorRequest,
    RegisterRequest,
    ResetPasswordRequest,
    StepUpResponse,
    TokenRequest,
    TokenResponse,
)
from documind.application.identity.auth_service import MfaChallenge
from documind.domain.errors import AuthenticationError

router = APIRouter(prefix="/auth", tags=["auth"])

_CHECK_EMAIL = "If that address can be used, we've sent an email with the next step."


def _refresh_cookie(request: Request, settings: SettingsDep) -> str | None:
    return request.cookies.get(settings.refresh_cookie_name)


RefreshCookie = Annotated[str | None, Depends(_refresh_cookie)]


@router.post(
    "/register",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MessageResponse,
    summary="Create an account (always 202, to prevent account enumeration)",
)
async def register(body: RegisterRequest, auth: AuthServiceDep) -> MessageResponse:
    await auth.register(str(body.email), body.password, body.display_name)
    return MessageResponse(detail="Check your email to verify your account.")


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(body: TokenRequest, auth: AuthServiceDep) -> MessageResponse:
    await auth.verify_email(body.token)
    return MessageResponse(detail="Your email is verified. You can sign in now.")


@router.post(
    "/verify-email/resend", status_code=status.HTTP_202_ACCEPTED, response_model=MessageResponse
)
async def resend_verification(body: EmailRequest, auth: AuthServiceDep) -> MessageResponse:
    await auth.resend_verification(str(body.email))
    return MessageResponse(detail=_CHECK_EMAIL)


@router.post(
    "/login",
    response_model=TokenResponse | MfaChallengeResponse,
    summary="Sign in (users with 2FA get a challenge to finish with /auth/login/mfa)",
)
async def login(
    body: LoginRequest, response: Response, auth: AuthServiceDep, settings: SettingsDep
) -> TokenResponse | MfaChallengeResponse:
    result = await auth.login(str(body.email), body.password)
    if isinstance(result, MfaChallenge):
        response.headers["Cache-Control"] = "no-store"
        return MfaChallengeResponse(
            challenge_token=result.challenge_token, expires_in=result.expires_in
        )
    return token_response(response, settings, result)


@router.post(
    "/login/mfa",
    response_model=TokenResponse,
    tags=["mfa"],
    summary="Finish signing in with an authenticator or recovery code",
)
async def login_mfa(
    body: MfaLoginRequest,
    response: Response,
    auth: AuthServiceDep,
    mfa: MfaServiceDep,
    settings: SettingsDep,
) -> TokenResponse:
    result = await auth.complete_mfa_login(
        body.challenge_token, body.code, body.recovery_code, mfa, email_code=body.email_code
    )
    return token_response(response, settings, result)


_EMAIL_SENT = "We sent a one-time code to your email address. It expires in a few minutes."


@router.post(
    "/login/mfa/email-code",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MessageResponse,
    tags=["mfa"],
    summary="During sign-in: email a one-time code instead of using the authenticator",
)
async def login_mfa_email_code(
    body: MfaChallengeTokenRequest, auth: AuthServiceDep, mfa: MfaServiceDep
) -> MessageResponse:
    await auth.send_login_email_code(body.challenge_token, mfa)
    return MessageResponse(detail=_EMAIL_SENT)


@router.post(
    "/mfa/email-code",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MessageResponse,
    tags=["mfa"],
    summary="Email a one-time code for step-up or for turning 2FA off (lost authenticator)",
)
async def mfa_email_code(user: CurrentUser, mfa: MfaServiceDep) -> MessageResponse:
    await mfa.send_email_code(user)
    return MessageResponse(detail=_EMAIL_SENT)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    dependencies=[Depends(require_csrf_header)],
    summary="Rotate the refresh cookie and issue a new access token",
)
async def refresh(
    response: Response, auth: AuthServiceDep, settings: SettingsDep, token: RefreshCookie
) -> TokenResponse | JSONResponse:
    try:
        result = await auth.refresh(token)
    except AuthenticationError as exc:
        # Build the 401 here: a raised error would get a fresh response from the
        # exception handler and lose the cookie deletion. Rate limiting (429)
        # propagates normally and keeps the cookie.
        error = JSONResponse(
            {"detail": exc.message, "code": exc.code},
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )
        clear_refresh_cookie(error, settings)
        return error
    return token_response(response, settings, result)


@router.post(
    "/logout", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_csrf_header)]
)
async def logout(
    response: Response, auth: AuthServiceDep, settings: SettingsDep, token: RefreshCookie
) -> Response:
    await auth.logout(token)
    response.status_code = status.HTTP_204_NO_CONTENT
    clear_refresh_cookie(response, settings)
    return response


@router.post(
    "/logout-all", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out on every device"
)
async def logout_all(
    response: Response, user: CurrentUser, auth: AuthServiceDep, settings: SettingsDep
) -> Response:
    await auth.logout_everywhere(user)
    response.status_code = status.HTTP_204_NO_CONTENT
    clear_refresh_cookie(response, settings)
    return response


@router.post(
    "/password/forgot", status_code=status.HTTP_202_ACCEPTED, response_model=MessageResponse
)
async def forgot_password(body: EmailRequest, auth: AuthServiceDep) -> MessageResponse:
    await auth.forgot_password(str(body.email))
    return MessageResponse(detail=_CHECK_EMAIL)


@router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(body: ResetPasswordRequest, auth: AuthServiceDep) -> Response:
    await auth.reset_password(body.token, body.new_password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/mfa/verify",
    response_model=StepUpResponse,
    tags=["mfa"],
    summary="Step-up: verify 2FA and get a short-lived elevated access token",
    description=(
        "Required before sensitive actions such as deleting documents. The returned access "
        "token proves a recent 2FA check for MFA_STEP_UP_TTL_SECONDS. The refresh cookie is "
        "not changed, so refreshed tokens are never elevated."
    ),
)
async def mfa_verify(
    body: MfaSecondFactorRequest,
    response: Response,
    user: CurrentUser,
    mfa: MfaServiceDep,
    container: ContainerDep,
) -> StepUpResponse:
    verified_at = await mfa.verify(user, body.code, body.recovery_code, body.email_code)
    issued = container.access_tokens.issue(user.id, user.auth_version, mfa_verified_at=verified_at)
    response.headers["Cache-Control"] = "no-store"
    return StepUpResponse(
        access_token=issued.token, expires_in=issued.expires_in, mfa_verified_at=verified_at
    )
