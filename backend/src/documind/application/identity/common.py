"""Types shared by identity use cases."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from documind.infrastructure.database.models import AuditLog, User
from documind.infrastructure.database.repositories.identity import AuditLogRepository


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, plus what their access token proves."""

    user: User
    mfa_verified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RequestMeta:
    """Who is calling, from where. Used for rate limiting, sessions and audit."""

    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None

    @property
    def client_key(self) -> str:
        return self.ip_address or "unknown"


@dataclass(slots=True)
class AuditRecorder:
    """Adds audit entries to the current unit of work (committed with the change)."""

    repo: AuditLogRepository
    meta: RequestMeta = field(default_factory=RequestMeta)

    def record(
        self,
        action: str,
        *,
        actor_user_id: uuid.UUID | None,
        workspace_id: uuid.UUID | None = None,
        target_type: str | None = None,
        target_id: uuid.UUID | str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.repo.add(
            AuditLog(
                action=action,
                actor_user_id=actor_user_id,
                workspace_id=workspace_id,
                target_type=target_type,
                target_id=str(target_id) if target_id is not None else None,
                details=details or {},
                ip_address=self.meta.ip_address,
                user_agent=(self.meta.user_agent or "")[:255] or None,
                request_id=self.meta.request_id,
            )
        )
