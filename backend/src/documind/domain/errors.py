"""Domain errors.

Each error carries a stable machine-readable ``code`` and a message that is
safe to show to API clients. The API layer maps error classes to HTTP status
codes; nothing here knows about HTTP.
"""

from __future__ import annotations


class DomainError(Exception):
    code: str = "error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class ValidationFailedError(DomainError):
    code = "validation_failed"


class AuthenticationError(DomainError):
    """Credentials or token are missing, invalid or expired."""

    code = "authentication_failed"


class EmailNotVerifiedError(DomainError):
    code = "email_not_verified"


class PermissionDeniedError(DomainError):
    code = "permission_denied"


class NotFoundError(DomainError):
    """Also used when a resource exists but the caller may not know that."""

    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class InvalidTokenError(DomainError):
    """A one-time token (verification, reset, invitation) is invalid or expired."""

    code = "invalid_token"


class PayloadTooLargeError(DomainError):
    code = "payload_too_large"


class UnsupportedMediaTypeError(DomainError):
    """The upload is not a supported, well-formed file of the type it claims to be."""

    code = "unsupported_file"


class ServiceUnavailableError(DomainError):
    code = "service_unavailable"


class RateLimitedError(DomainError):
    code = "rate_limited"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Too many requests. Please try again later.")
        self.retry_after_seconds = retry_after_seconds


class QuotaExceededError(DomainError):
    """A plan limit was reached (ADR 0020). Mapped to 402 with the quota details."""

    code = "quota_exceeded"

    def __init__(
        self, message: str, *, metric: str, limit: int, used: float, resets_at: str | None
    ) -> None:
        super().__init__(message)
        self.metric = metric
        self.limit = limit
        self.used = used
        self.resets_at = resets_at
