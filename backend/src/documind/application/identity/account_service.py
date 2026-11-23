"""The signed-in user's own account: profile and password."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity import emails
from documind.application.identity.auth_service import AuthResult, AuthService
from documind.application.identity.common import AuditRecorder, RequestMeta, utcnow
from documind.application.ports import EmailSender, PasswordHasher, RateLimiter
from documind.core.config import Settings
from documind.domain.errors import AuthenticationError, ValidationFailedError
from documind.domain.identity.values import clean_display_name, validate_password
from documind.infrastructure.database.models import User
from documind.infrastructure.database.repositories.identity import (
    AuditLogRepository,
    UserRepository,
)


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
