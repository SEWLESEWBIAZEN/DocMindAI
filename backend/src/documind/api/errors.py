"""Map domain errors to HTTP responses: ``{"detail": str, "code": str}``."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from documind.domain.errors import (
    AuthenticationError,
    ConflictError,
    DomainError,
    EmailNotVerifiedError,
    InvalidTokenError,
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
    QuotaExceededError,
    RateLimitedError,
    ServiceUnavailableError,
    UnsupportedMediaTypeError,
    ValidationFailedError,
)

_STATUS: list[tuple[type[DomainError], int]] = [
    (RateLimitedError, 429),
    (QuotaExceededError, 402),
    (AuthenticationError, 401),
    (EmailNotVerifiedError, 403),
    (PermissionDeniedError, 403),
    (NotFoundError, 404),
    (ConflictError, 409),
    (InvalidTokenError, 400),
    (PayloadTooLargeError, 413),
    (UnsupportedMediaTypeError, 415),
    (ServiceUnavailableError, 503),
    (ValidationFailedError, 422),
]


def status_for(error: DomainError) -> int:
    for cls, status in _STATUS:
        if isinstance(error, cls):
            return status
    return 400


async def _handle_domain_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)  # noqa: S101 - registered only for DomainError
    headers: dict[str, str] = {}
    if isinstance(exc, RateLimitedError):
        headers["Retry-After"] = str(exc.retry_after_seconds)
    if isinstance(exc, AuthenticationError):
        headers["WWW-Authenticate"] = "Bearer"
    body: dict[str, object] = {"detail": exc.message, "code": exc.code}
    if isinstance(exc, QuotaExceededError):
        body["quota"] = {
            "metric": exc.metric,
            "limit": exc.limit,
            "used": exc.used,
            "resets_at": exc.resets_at,
        }
    return JSONResponse(body, status_code=status_for(exc), headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _handle_domain_error)
