"""The signed-in user's own account: profile, password and email (ADR 0039)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity import emails
from documind.application.identity.auth_service import AuthResult, AuthService
from documind.application.identity.common import AuditRecorder, Principal, RequestMeta, utcnow
from documind.application.identity.deletion import ensure_recent_account_mfa
from documind.application.identity.sso_service import sso_required_for
from documind.application.ports import EmailSender, PasswordHasher, RateLimiter
from documind.core.config import Settings
from documind.domain.errors import (
    AuthenticationError,
    ConflictError,
    InvalidTokenError,
    ValidationFailedError,
)
from documind.domain.identity.tokens import generate_token, hash_token
from documind.domain.identity.values import (
    clean_display_name,
    normalize_email,
    validate_password,
)
from documind.infrastructure.database.models import AccountToken, User
from documind.infrastructure.database.repositories.identity import (
    AccountTokenRepository,
    AuditLogRepository,
    UserRepository,
)

EMAIL_CHANGE = "email_change"


class AccountService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        hasher: PasswordHasher,
        email: EmailSender,
        limiter: RateLimiter,
        auth: AuthService,
        meta: RequestMeta,
    ) -> None:
        self._session = session
        self._settings = settings
        self._hasher = hasher
        self._email = email
        self._limiter = limiter
        self._auth = auth
        self._users = UserRepository(session)
        self._tokens = AccountTokenRepository(session)
        self._audit = AuditRecorder(AuditLogRepository(session), meta)

    async def update_profile(self, user: User, display_name: str) -> User:
        locked = await self._users.require(user.id, for_update=True)
        locked.display_name = clean_display_name(display_name)
        self._audit.record(
            "user.profile_updated", actor_user_id=user.id, target_type="user", target_id=user.id
        )
        await self._session.commit()
        return locked

    async def change_password(
        self, user: User, current_password: str, new_password: str
    ) -> AuthResult:
        """Change password, sign out every other session, and start a fresh one here."""
        s = self._settings
        # Stops a stolen access token from being used to guess the current password.
        await self._limiter.hit(
            "password-change:user",
            str(user.id),
            s.rate_limit_login_per_email,
            s.rate_limit_login_window_seconds,
        )
        locked = await self._users.require(user.id, for_update=True)
        if not await self._hasher.verify(locked.password_hash, current_password):
            raise AuthenticationError(
                "Your current password is incorrect.", code="invalid_credentials"
            )
        if current_password == new_password:
            raise ValidationFailedError("Choose a password you haven't used here before.")
        validate_password(new_password, email=locked.email)
        await self._auth.apply_new_password(locked, new_password, utcnow())
        self._audit.record(
            "user.password_changed", actor_user_id=user.id, target_type="user", target_id=user.id
        )
        result = await self._auth.start_session_for(locked)  # commits
        await self._email.send(emails.password_changed_email(locked.email, locked.display_name))
        return result

    # ------------------------------------------------------------- email change

    async def request_email_change(
        self, principal: Principal, new_email: str, password: str
    ) -> None:
        """Send a confirmation link to the new address; nothing changes until it's opened.

        The answer is the same whether or not the new address already has an account,
        so this can't be used to find out who uses DocuMind.
        """
        s = self._settings
        user = principal.user
        await self._limiter.hit(
            "email-change:user",
            str(user.id),
            s.rate_limit_login_per_email,
            s.rate_limit_login_window_seconds,
        )
        locked = await self._users.require(user.id, for_update=True)
        if not await self._hasher.verify(locked.password_hash, password):
            raise AuthenticationError("Your password is incorrect.", code="invalid_credentials")
        ensure_recent_account_mfa(principal, utcnow(), s.mfa_step_up_ttl_seconds)
        target = normalize_email(new_email)
        if target == locked.email:
            raise ValidationFailedError("That's already your email address.")
        if await sso_required_for(self._session, locked.email) is not None:
            raise ConflictError(
                "Your organization manages your email through single sign-on. Ask its admin "
                "to change it.",
                code="email_managed_by_sso",
            )
        if await self._users.get_by_email(target) is not None:
            await self._email.send(emails.email_taken_email(target, s.public_app_url))
            return
        ttl = s.email_change_ttl_hours
        raw = generate_token()
        self._tokens.add(
            AccountToken(
                user_id=locked.id,
                purpose=EMAIL_CHANGE,
                token_hash=hash_token(raw),
                expires_at=utcnow() + timedelta(hours=ttl),
                new_email=target,
            )
        )
        self._audit.record(
            "user.email_change_requested",
            actor_user_id=locked.id,
            target_type="user",
            target_id=locked.id,
            details={"new_domain": target.partition("@")[2]},
        )
        await self._session.commit()
        await self._email.send(
            emails.email_change_confirm_email(
                target, locked.display_name, s.public_app_url, raw, ttl
            )
        )
        await self._email.send(
            emails.email_change_requested_email(locked.email, locked.display_name, target)
        )

    async def confirm_email_change(self, token: str) -> User:
        """Opened from the new mailbox: switch the account to that address."""
        record = await self._tokens.get_by_hash(hash_token(token), EMAIL_CHANGE)
        now = utcnow()
        if (
            record is None
            or record.used_at is not None
            or record.expires_at <= now
            or not record.new_email
        ):
            raise InvalidTokenError("This link is invalid or has expired.")
        user = await self._users.require(record.user_id, for_update=True)
        if not user.is_active:
            raise InvalidTokenError("This link is invalid or has expired.")
        old, new = user.email, record.new_email
        record.used_at = now
        user.email = new
        # Opening the link proves control of the new address.
        user.email_verified_at = now
        self._audit.record(
            "user.email_changed",
            actor_user_id=user.id,
            target_type="user",
            target_id=user.id,
            details={"old_domain": old.partition("@")[2], "new_domain": new.partition("@")[2]},
        )
        try:
            await self._session.commit()
        except IntegrityError as exc:  # someone registered the address meanwhile
            await self._session.rollback()
            raise ConflictError(
                "That address now belongs to another account.", code="email_taken"
            ) from exc
        await self._email.send(emails.email_changed_email(old, user.display_name, new))
        return user
