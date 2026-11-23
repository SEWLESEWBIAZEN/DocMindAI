"""Ports (interfaces) the application layer depends on. Adapters live in infrastructure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


class PasswordHasher(Protocol):
    async def hash(self, password: str) -> str: ...

    async def verify(self, password_hash: str, password: str) -> bool: ...

    def needs_rehash(self, password_hash: str) -> bool: ...

    async def verify_dummy(self, password: str) -> None:
        """Spend the same time as a real verification (for unknown accounts)."""
        ...


@dataclass(frozen=True, slots=True)
class IssuedAccessToken:
    token: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: UUID
    auth_version: int


class AccessTokenService(Protocol):
    def issue(self, user_id: UUID, auth_version: int) -> IssuedAccessToken: ...

    def decode(self, token: str) -> AccessTokenClaims:
        """Raise AuthenticationError if the token is invalid or expired."""
        ...


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    html: str


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> None: ...


class RateLimiter(Protocol):
    async def hit(self, scope: str, identifier: str, limit: int, window_seconds: int) -> None:
        """Count one request; raise RateLimitedError when over the limit."""
        ...
