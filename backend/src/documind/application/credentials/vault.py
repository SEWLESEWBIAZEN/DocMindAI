"""Credential vault (ADR 0022): store, use, revoke and rewrap customer secrets."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity.common import utcnow
from documind.domain.errors import ServiceUnavailableError
from documind.infrastructure.database.models import Credential
from documind.infrastructure.security.vault import EnvelopeCipher, Sealed, VaultError

logger = logging.getLogger(__name__)

KINDS = frozenset(
    {"ai_provider_key", "database_password", "storage_secret", "worker_token", "connector_token"}
)
_TOUCH_EVERY = timedelta(hours=1)


def hint_for(secret: str) -> str:
    """The last 4 characters, never more (and nothing for short secrets)."""
    return f"…{secret[-4:]}" if len(secret) >= 12 else "…"


def _aad(credential_id: uuid.UUID, workspace_id: uuid.UUID, kind: str) -> str:
    return f"documind-credential:v1|{credential_id}|{workspace_id}|{kind}"


class CredentialVault:
    """Every customer secret goes through here. Plaintext never leaves memory."""

    def __init__(self, session: AsyncSession, cipher: EnvelopeCipher | None) -> None:
        self._s = session
        self._cipher = cipher

    def _require(self) -> EnvelopeCipher:
        if self._cipher is None:
            raise ServiceUnavailableError(
                "Secure credential storage isn't configured on this server.",
                code="credential_vault_not_configured",
            )
        return self._cipher

    def store(
        self, workspace_id: uuid.UUID, kind: str, secret: str, *, created_by: uuid.UUID | None
    ) -> Credential:
        """Encrypt and add a new credential to the session (caller commits)."""
        if kind not in KINDS:
            raise ValueError(f"unknown credential kind {kind!r}")
        cipher = self._require()
        credential_id = uuid.uuid4()
        sealed = cipher.seal(secret, _aad(credential_id, workspace_id, kind))
        row = Credential(
            id=credential_id,
            workspace_id=workspace_id,
            kind=kind,
            ciphertext=sealed.ciphertext,
            wrapped_dek=sealed.wrapped_dek,
            kek_id=sealed.kek_id,
            hint=hint_for(secret),
            created_by_user_id=created_by,
        )
        self._s.add(row)
        return row

    def reveal(self, row: Credential) -> str:
        """Decrypt for immediate use. Raises ``VaultError`` (no secret in the message)."""
        if row.status != "active":
            raise VaultError("Credential has been revoked")
        cipher = self._require()
        secret = cipher.open(
            Sealed(row.ciphertext, row.wrapped_dek, row.kek_id),
            _aad(row.id, row.workspace_id, row.kind),
        )
        now = utcnow()
        if row.last_used_at is None or now - row.last_used_at > _TOUCH_EVERY:
            row.last_used_at = now
        return secret

    async def get(self, workspace_id: uuid.UUID, credential_id: uuid.UUID) -> Credential | None:
        return (
            await self._s.execute(
                select(Credential).where(
                    Credential.id == credential_id, Credential.workspace_id == workspace_id
                )
            )
        ).scalar_one_or_none()

    @staticmethod
    def revoke(row: Credential) -> None:
        """Revoke and destroy the ciphertext (the row stays for audit references)."""
        row.status = "revoked"
        row.revoked_at = utcnow()
        row.ciphertext = ""
        row.wrapped_dek = ""

    async def rewrap_all(self, *, batch: int = 500) -> tuple[int, int]:
        """Re-wrap every active credential's data key with the primary KEK.

        Returns (rewrapped, already current). Run after adding a new key first
        in ``CREDENTIAL_ENCRYPTION_KEYS``; the old key can be removed afterwards.
        """
        cipher = self._require()
        done = current = 0
        rows = (
            await self._s.execute(
                select(Credential)
                .where(Credential.status == "active")
                .order_by(Credential.created_at)
                .with_for_update()
            )
        ).scalars()
        for row in rows:
            if row.kek_id == cipher.primary_id:
                current += 1
                continue
            sealed = cipher.rewrap(
                Sealed(row.ciphertext, row.wrapped_dek, row.kek_id),
                _aad(row.id, row.workspace_id, row.kind),
            )
            row.wrapped_dek, row.kek_id = sealed.wrapped_dek, sealed.kek_id
            row.rotated_at = utcnow()
            done += 1
            if done % batch == 0:
                await self._s.flush()
        await self._s.commit()
        logger.info("credentials rewrapped", extra={"rewrapped": done, "current": current})
        return done, current
