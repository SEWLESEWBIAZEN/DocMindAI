"""Support-assisted 2FA reset (ADR 0030).

For someone who lost every factor: authenticator, passkeys, recovery codes and
their email. They prove the password (the sign-in challenge) and ask for a
reset; support verifies their identity outside the app and approves or denies.
An approved reset only takes effect after ``MFA_RESET_DELAY_HOURS``, and is
cancelled if anyone signs in with real 2FA in the meantime, which is what an
account owner targeted by a social-engineering attempt would do. Every step is
audited and emailed to the account.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity import emails
from documind.application.identity.common import AuditRecorder, RequestMeta, utcnow
from documind.application.ports import EmailSender
from documind.domain.errors import ConflictError, NotFoundError, ValidationFailedError
from documind.infrastructure.database.models import (
    MfaRecoveryCode,
    MfaResetRequest,
    User,
    WebAuthnCredential,
)
from documind.infrastructure.database.repositories.identity import AuditLogRepository

OPEN = ("pending", "approved")


@dataclass(frozen=True, slots=True)
class ResetView:
    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    status: str
    reason: str
    created_at: datetime
    ip_address: str | None
    decided_by: str | None
    effective_at: datetime | None


def clear_all_factors(user: User) -> None:
    """Turn 2FA off completely (caller deletes passkeys and recovery codes)."""
    user.totp_secret_encrypted = None
    user.totp_enabled_at = None
    user.totp_last_used_step = None
    user.totp_pending_secret_encrypted = None
    user.totp_pending_expires_at = None
    user.mfa_email_enabled_at = None
    user.mfa_passkey_enabled_at = None


async def delete_factor_rows(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(delete(WebAuthnCredential).where(WebAuthnCredential.user_id == user_id))
    await session.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user_id))


class MfaResetService:
    def __init__(
        self,
        session: AsyncSession,
        email: EmailSender,
        meta: RequestMeta,
        *,
        delay_hours: int = 72,
    ) -> None:
        self._s = session
        self._email = email
        self._meta = meta
        self._delay = timedelta(hours=delay_hours)
        self._audit = AuditRecorder(AuditLogRepository(session), meta)

    async def _open(self, user_id: uuid.UUID) -> list[MfaResetRequest]:
        rows = await self._s.execute(
            select(MfaResetRequest)
            .where(MfaResetRequest.user_id == user_id, MfaResetRequest.status.in_(OPEN))
            .with_for_update()
        )
        return list(rows.scalars())

    # ------------------------------------------------------------ the user

    async def request(self, user: User, reason: str) -> None:
        """Ask support for a reset. The caller has proved the password (sign-in challenge)."""
        reason = " ".join(reason.split())
        if not 10 <= len(reason) <= 1000:
            raise ValidationFailedError(
                "Tell us briefly what happened (10 to 1000 characters), so support can verify it."
            )
        if await self._open(user.id):
            raise ConflictError(
                "A reset request is already open for this account. Support will contact you.",
                code="mfa_reset_open",
            )
        row = MfaResetRequest(
            user_id=user.id,
            reason=reason,
            ip_address=self._meta.ip_address,
            user_agent=(self._meta.user_agent or "")[:255] or None,
        )
        self._s.add(row)
        await self._s.flush()
        self._audit.record(
            "user.mfa_reset_requested",
            actor_user_id=user.id,
            target_type="mfa_reset_request",
            target_id=row.id,
        )
        await self._s.commit()
        await self._email.send(emails.mfa_reset_requested_email(user.email, user.display_name))

    async def cancel_open(self, user: User, why: str) -> None:
        """Real 2FA was used: whoever asked for a reset doesn't need one. Caller commits."""
        rows = await self._open(user.id)
        for row in rows:
            row.status = "cancelled"
            row.decision_note = why[:500]
            row.decided_at = utcnow()
            self._audit.record(
                "user.mfa_reset_cancelled",
                actor_user_id=user.id,
                target_type="mfa_reset_request",
                target_id=row.id,
                details={"why": why},
            )
        if rows:
            await self._s.flush()
            await self._email.send(emails.mfa_reset_cancelled_email(user.email, user.display_name))

    async def apply_if_due(self, user: User) -> bool:
        """At sign-in: an approved reset whose waiting period is over turns 2FA off."""
        now = utcnow()
        due = [r for r in await self._open(user.id) if r.status == "approved"]
        due = [r for r in due if r.effective_at is not None and r.effective_at <= now]
        if not due:
            return False
        clear_all_factors(user)
        await delete_factor_rows(self._s, user.id)
        for row in due:
            row.status = "completed"
            row.completed_at = now
            self._audit.record(
                "user.mfa_reset_completed",
                actor_user_id=user.id,
                target_type="mfa_reset_request",
                target_id=row.id,
            )
        await self._s.flush()
        await self._email.send(emails.mfa_reset_completed_email(user.email, user.display_name))
        return True

    # ------------------------------------------------------------- support

    async def list(self, status: str | None = None, limit: int = 100) -> list[ResetView]:
        stmt = (
            select(MfaResetRequest, User.email)
            .join(User, User.id == MfaResetRequest.user_id)
            .order_by(MfaResetRequest.created_at.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(MfaResetRequest.status == status)
        return [
            ResetView(
                r.id,
                r.user_id,
                email,
                r.status,
                r.reason,
                r.created_at,
                r.ip_address,
                r.decided_by,
                r.effective_at,
            )
            for r, email in (await self._s.execute(stmt)).all()
        ]

    async def _decide(self, request_id: uuid.UUID, operator: str) -> tuple[MfaResetRequest, User]:
        if not operator.strip():
            raise ValidationFailedError("Name the operator who verified the request.")
        row = await self._s.get(MfaResetRequest, request_id, with_for_update=True)
        if row is None:
            raise NotFoundError("Reset request not found.")
        if row.status != "pending":
            raise ConflictError(f"This request is {row.status}.", code="mfa_reset_not_pending")
        user = await self._s.get(User, row.user_id)
        assert user is not None  # noqa: S101 - foreign key
        return row, user

    async def approve(self, request_id: uuid.UUID, operator: str, note: str | None) -> datetime:
        """Approve after verifying identity out of band. Takes effect after the delay."""
        row, user = await self._decide(request_id, operator)
        now = utcnow()
        row.status = "approved"
        row.decided_at = now
        row.decided_by = operator.strip()[:128]
        row.decision_note = (note or "")[:500] or None
        row.effective_at = now + self._delay
        self._audit.record(
            "user.mfa_reset_approved",
            actor_user_id=None,
            target_type="mfa_reset_request",
            target_id=row.id,
            details={"operator": row.decided_by, "effective_at": row.effective_at.isoformat()},
        )
        await self._s.commit()
        await self._email.send(
            emails.mfa_reset_approved_email(user.email, user.display_name, row.effective_at)
        )
        return row.effective_at

    async def deny(self, request_id: uuid.UUID, operator: str, note: str | None) -> None:
        row, user = await self._decide(request_id, operator)
        row.status = "denied"
        row.decided_at = utcnow()
        row.decided_by = operator.strip()[:128]
        row.decision_note = (note or "")[:500] or None
        self._audit.record(
            "user.mfa_reset_denied",
            actor_user_id=None,
            target_type="mfa_reset_request",
            target_id=row.id,
            details={"operator": row.decided_by},
        )
        await self._s.commit()
        await self._email.send(emails.mfa_reset_denied_email(user.email, user.display_name))
