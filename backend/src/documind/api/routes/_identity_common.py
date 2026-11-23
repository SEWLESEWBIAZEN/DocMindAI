"""Helpers shared by identity routes."""

from __future__ import annotations

from fastapi import Response

from documind.api.schemas.identity import TokenResponse, UserResponse
from documind.application.identity.auth_service import AuthResult
from documind.core.config import Settings
from documind.infrastructure.database.models import User

REFRESH_COOKIE_PATH = "/api/v1/auth"


def user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        email_verified=user.email_verified_at is not None,
        mfa_enabled=user.mfa_enabled,
        default_workspace_id=user.default_workspace_id,
        created_at=user.created_at,
    )


def set_refresh_cookie(response: Response, settings: Settings, result: AuthResult) -> None:
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=result.refresh_token,
        expires=result.refresh_expires_at,
        path=REFRESH_COOKIE_PATH,  # only sent to auth endpoints
        secure=settings.auth_cookie_secure,
        httponly=True,  # unreadable by JavaScript
        samesite="strict",
    )


def clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path=REFRESH_COOKIE_PATH,
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def token_response(response: Response, settings: Settings, result: AuthResult) -> TokenResponse:
    set_refresh_cookie(response, settings, result)
    response.headers["Cache-Control"] = "no-store"
    return TokenResponse(
        access_token=result.access_token,
        expires_in=result.access_expires_in,
        user=user_response(result.user),
    )
