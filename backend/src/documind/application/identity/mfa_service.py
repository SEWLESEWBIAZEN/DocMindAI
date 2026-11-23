"""Two-factor authentication (TOTP + recovery codes) and step-up verification.

* Enrollment needs the account password, then a valid code from the new
  authenticator before 2FA is switched on.
* Step-up (``verify``) proves possession of the second factor; the API then
  issues a short-lived access token carrying the verification time, which
  sensitive actions such as deleting documents require (ADR 0013).
* Every attempt is rate limited per user; failures are audited.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity import emails
from documind.application.identity.common import AuditRecorder, RequestMeta, utcnow
from documind.application.ports import EmailSender, PasswordHasher, RateLimiter
from documind.core.config import Settings, mfa_key_bytes
from documind.domain.errors import (
    AuthenticationError,
    ConflictError,
    InvalidTokenError,
    PermissionDeniedError,
    ValidationFailedError,
)
from documind.domain.identity import totp
from documind.infrastructure.database.models import User
from documind.infrastructure.database.repositories.identity import (
    AuditLogRepository,
    MfaRecoveryCodeRepository,
    UserRepository,
)
from documind.infrastructure.security.secret_box import SecretBox


def _totp_clock() -> float:
    """Wall clock for TOTP; a separate function so tests can move time forward."""
    return time.time()


@dataclass(frozen=True, slots=True)
class MfaStatus:
    enabled: bool
    enabled_at: datetime | None
    recovery_codes_remaining: int


@dataclass(frozen=True, slots=True)
class TotpSetup:
    secret: str
    otpauth_uri: str
    expires_at: datetime


class MfaService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        hasher: PasswordHasher,
        box: SecretBox,
        email: EmailSender,
        limiter: RateLimiter,
        meta: RequestMeta,
    ) -> None:
        self._session = session
        self._settings = settings
        self._hasher = hasher
        self._box = box
        self._email = email
        self._limiter = limiter
        self._pepper = mfa_key_bytes(settings.mfa_encryption_key)
        self._users = UserRepository(session)
        self._codes = MfaRecoveryCodeRepository(session)
        self._audit = AuditRecorder(AuditLogRepository(session), meta)

    # ----------------------------------------------------------------- helpers

    async def _limit(self, user: User) -> None:
        s = self._settings
        await self._limiter.hit(
            "mfa:user", str(user.id), s.rate_limit_mfa_per_user, s.rate_limit_mfa_window_seconds
        )

    @staticmethod
    def _context(user: User) -> str:
        return f"totp:{user.id}"

    async def _require_password(self, user: User, password: str) -> None:
        if not await self._hasher.verify(user.password_hash, password):
            self._audit.record(
                "auth.mfa_failed", actor_user_id=user.id, details={"reason": "password"}
            )
            await self._session.commit()
            raise AuthenticationError("Your password is incorrect.", code="invalid_credentials")

    async def _new_recovery_codes(self, user: User) -> list[str]:
        codes = totp.generate_recovery_codes()
        await self._codes.replace_all(
            user.id, [totp.hash_recovery_code(c, self._pepper) for c in codes]
        )
        return codes

    async def _check_second_factor(
        self, user: User, code: str | None, recovery_code: str | None
    ) -> str:
        """Verify a TOTP code or a recovery code; returns the method used. Caller commits."""
        if (code is None) == (recovery_code is None):
            raise ValidationFailedError("Provide either an authenticator code or a recovery code.")
        if code is not None and user.totp_secret_encrypted:
            secret = self._box.decrypt(user.totp_secret_encrypted, context=self._context(user))
            step = totp.verify(secret, code, _totp_clock(), last_used_step=user.totp_last_used_step)
            if step is not None:
                user.totp_last_used_step = step  # prevents replay of this and older codes
                return "totp"
        elif recovery_code is not None:
            digest = totp.hash_recovery_code(recovery_code, self._pepper)
            if await self._codes.consume(user.id, digest, utcnow()):
                return "recovery_code"
        self._audit.record("auth.mfa_failed", actor_user_id=user.id, details={"reason": "code"})
        await self._session.commit()
        raise AuthenticationError(
            "That code isn't valid. Try the latest code from your app.", code="invalid_mfa_code"
        )

    # ----------------------------------------------------------------- queries

    async def status(self, user: User) -> MfaStatus:
        return MfaStatus(
            enabled=user.mfa_enabled,
            enabled_at=user.totp_enabled_at,
            recovery_codes_remaining=await self._codes.remaining(user.id)
            if user.mfa_enabled
            else 0,
        )

    # -------------------------------------------------------------- enrollment

    async def begin_setup(self, user: User, password: str) -> TotpSetup:
        await self._limit(user)
        locked = await self._users.require(user.id, for_update=True)
        await self._require_password(locked, password)
        if locked.mfa_enabled:
            raise ConflictError(
                "Two-factor authentication is already on.", code="mfa_already_enabled"
            )
        secret = totp.generate_secret()
        expires = utcnow() + timedelta(minutes=self._settings.mfa_setup_ttl_minutes)
        locked.totp_pending_secret_encrypted = self._box.encrypt(
            secret, context=self._context(locked)
        )
        locked.totp_pending_expires_at = expires
        self._audit.record(
            "user.mfa_setup_started",
            actor_user_id=locked.id,
            target_type="user",
            target_id=locked.id,
        )
        await self._session.commit()
        return TotpSetup(
            secret=secret,
            otpauth_uri=totp.provisioning_uri(secret, locked.email, self._settings.mfa_issuer),
            expires_at=expires,
        )

    async def confirm_setup(self, user: User, code: str) -> list[str]:
        await self._limit(user)
        locked = await self._users.require(user.id, for_update=True)
        if locked.mfa_enabled:
            raise ConflictError(
                "Two-factor authentication is already on.", code="mfa_already_enabled"
            )
        now = utcnow()
        if (
            not locked.totp_pending_secret_encrypted
            or locked.totp_pending_expires_at is None
            or locked.totp_pending_expires_at <= now
        ):
            raise InvalidTokenError(
                "No two-factor setup is in progress, or it expired. Start again."
            )
        secret = self._box.decrypt(
            locked.totp_pending_secret_encrypted, context=self._context(locked)
        )
        step = totp.verify(secret, code, _totp_clock(), last_used_step=None)
        if step is None:
            self._audit.record(
                "auth.mfa_failed", actor_user_id=locked.id, details={"reason": "setup_code"}
            )
            await self._session.commit()
            raise AuthenticationError(
                "That code isn't valid. Check your authenticator app.", code="invalid_mfa_code"
            )

        locked.totp_secret_encrypted = locked.totp_pending_secret_encrypted
        locked.totp_enabled_at = now
        locked.totp_last_used_step = step
        locked.totp_pending_secret_encrypted = None
        locked.totp_pending_expires_at = None
        codes = await self._new_recovery_codes(locked)
        self._audit.record(
            "user.mfa_enabled", actor_user_id=locked.id, target_type="user", target_id=locked.id
        )
        await self._session.commit()
        await self._email.send(emails.mfa_enabled_email(locked.email, locked.display_name))
        return codes

    async def disable(
        self, user: User, password: str, code: str | None, recovery_code: str | None
    ) -> None:
        await self._limit(user)
        locked = await self._users.require(user.id, for_update=True)
        await self._require_password(locked, password)
        if not locked.mfa_enabled:
            raise ConflictError("Two-factor authentication is not on.", code="mfa_not_enabled")
        await self._check_second_factor(locked, code, recovery_code)
        locked.totp_secret_encrypted = None
        locked.totp_enabled_at = None
        locked.totp_last_used_step = None
        await self._codes.delete_all(locked.id)
        self._audit.record(
            "user.mfa_disabled", actor_user_id=locked.id, target_type="user", target_id=locked.id
        )
        await self._session.commit()
        await self._email.send(emails.mfa_disabled_email(locked.email, locked.display_name))

    async def regenerate_recovery_codes(self, user: User, code: str) -> list[str]:
        await self._limit(user)
        locked = await self._users.require(user.id, for_update=True)
        if not locked.mfa_enabled:
            raise ConflictError("Two-factor authentication is not on.", code="mfa_not_enabled")
        await self._check_second_factor(locked, code, None)
        codes = await self._new_recovery_codes(locked)
        self._audit.record(
            "user.mfa_recovery_codes_regenerated",
            actor_user_id=locked.id,
            target_type="user",
            target_id=locked.id,
        )
        await self._session.commit()
        return codes

    # ----------------------------------------------------------------- step-up

    async def verify(self, user: User, code: str | None, recovery_code: str | None) -> datetime:
        """Check the second factor; returns the verification time for the step-up token."""
        await self._limit(user)
        locked = await self._users.require(user.id, for_update=True)
        if not locked.mfa_enabled:
            raise PermissionDeniedError(
                "Set up two-factor authentication first.", code="mfa_enrollment_required"
            )
        method = await self._check_second_factor(locked, code, recovery_code)
        now = utcnow()
        self._audit.record("auth.mfa_verified", actor_user_id=locked.id, details={"method": method})
        await self._session.commit()
        if method == "recovery_code":
            remaining = await self._codes.remaining(locked.id)
            await self._email.send(
                emails.recovery_code_used_email(locked.email, locked.display_name, remaining)
            )
        return now
