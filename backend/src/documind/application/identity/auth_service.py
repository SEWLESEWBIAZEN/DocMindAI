"""Authentication use cases: registration, verification, sessions, password reset.

Security properties (see ADR 0008):

* No account enumeration: register, resend-verification and forgot-password
  behave identically whether or not the email exists; login spends the same
  Argon2 time for unknown emails.
* Refresh tokens rotate on every use. Presenting an already-rotated token
  (outside a short grace window for concurrent tabs) revokes the whole
  session family, because it indicates the token was stolen.
* Password changes and "sign out everywhere" bump ``users.auth_version``,
  which immediately invalidates outstanding access tokens.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity import emails
from documind.application.identity.common import AuditRecorder, Principal, RequestMeta, utcnow
from documind.application.ports import AccessTokenService, EmailSender, PasswordHasher, RateLimiter
from documind.core.config import Settings
from documind.domain.errors import (
    AuthenticationError,
    EmailNotVerifiedError,
    InvalidTokenError,
)
from documind.domain.identity.permissions import RoleKey
from documind.domain.identity.policies import WorkspaceKind
from documind.domain.identity.tokens import generate_token, hash_token
from documind.domain.identity.values import (
    clean_display_name,
    normalize_email,
    validate_password,
)
from documind.infrastructure.database.models import (
    AccountToken,
    RefreshToken,
    User,
    Workspace,
    WorkspaceMember,
)
from documind.infrastructure.database.repositories.identity import (
    AccountTokenRepository,
    AuditLogRepository,
    RefreshTokenRepository,
    RoleRepository,
    UserRepository,
    WorkspaceRepository,
)

logger = logging.getLogger(__name__)

EMAIL_VERIFICATION = "email_verification"
PASSWORD_RESET = "password_reset"  # noqa: S105 - token purpose label, not a secret
# Concurrent refreshes from two tabs can present the same token twice; within
# this window the second is rejected without treating it as theft.
ROTATION_GRACE = timedelta(seconds=10)


@dataclass(frozen=True, slots=True)
class AuthResult:
    user: User
    access_token: str
    access_expires_in: int
    refresh_token: str
    refresh_expires_at: datetime


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        hasher: PasswordHasher,
        tokens: AccessTokenService,
        email: EmailSender,
        limiter: RateLimiter,
        meta: RequestMeta,
    ) -> None:
        self._session = session
        self._settings = settings
        self._hasher = hasher
        self._tokens = tokens
        self._email = email
        self._limiter = limiter
        self._meta = meta
        self._users = UserRepository(session)
        self._roles = RoleRepository(session)
        self._workspaces = WorkspaceRepository(session)
        self._refresh = RefreshTokenRepository(session)
        self._account_tokens = AccountTokenRepository(session)
        self._audit = AuditRecorder(AuditLogRepository(session), meta)

    # ------------------------------------------------------------------ helpers

    async def _limit_auth(self, scope: str, email: str | None = None) -> None:
        s = self._settings
        await self._limiter.hit(
            f"{scope}:ip",
            self._meta.client_key,
            s.rate_limit_auth_per_ip,
            s.rate_limit_auth_window_seconds,
        )
        if email:
            await self._limiter.hit(
                f"{scope}:email", email, s.rate_limit_auth_per_ip, s.rate_limit_auth_window_seconds
            )

    def _new_account_token(self, user: User, purpose: str, ttl: timedelta) -> str:
        raw = generate_token()
        self._account_tokens.add(
            AccountToken(
                user_id=user.id,
                purpose=purpose,
                token_hash=hash_token(raw),
                expires_at=utcnow() + ttl,
            )
        )
        return raw

    def _start_session(
        self, user: User, family_id: uuid.UUID | None = None
    ) -> tuple[RefreshToken, str]:
        raw = generate_token()
        token = RefreshToken(
            id=uuid.uuid4(),
            user_id=user.id,
            family_id=family_id or uuid.uuid4(),
            token_hash=hash_token(raw),
            expires_at=utcnow() + timedelta(days=self._settings.refresh_token_ttl_days),
            user_agent=(self._meta.user_agent or "")[:255] or None,
            ip_address=self._meta.ip_address,
        )
        self._refresh.add(token)
        return token, raw

    def _result(self, user: User, refresh: RefreshToken, raw_refresh: str) -> AuthResult:
        access = self._tokens.issue(user.id, user.auth_version)
        return AuthResult(
            user=user,
            access_token=access.token,
            access_expires_in=access.expires_in,
            refresh_token=raw_refresh,
            refresh_expires_at=refresh.expires_at,
        )

    # ------------------------------------------------------------ registration

    async def register(self, email: str, password: str, display_name: str) -> None:
        """Create an account and its personal workspace, or quietly notify an existing owner."""
        email = normalize_email(email)
        await self._limit_auth("register", email)
        name = clean_display_name(display_name)
        validate_password(password, email=email)
        # Always hash, so response time doesn't reveal whether the email exists.
        password_hash = await self._hasher.hash(password)

        existing = await self._users.get_by_email(email)
        if existing is not None:
            await self._notify_existing_account(existing)
            return

        user = User(id=uuid.uuid4(), email=email, password_hash=password_hash, display_name=name)
        workspace = Workspace(
            id=uuid.uuid4(),
            kind=WorkspaceKind.PERSONAL.value,
            name="Personal",
            personal_owner_id=user.id,
        )
        owner = await self._roles.get_by_key(RoleKey.OWNER)
        self._users.add(user)
        await self._session.flush()  # users row must exist before the workspace FK
        self._workspaces.add(workspace)
        await self._session.flush()
        self._workspaces.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role_id=owner.id)
        )
        user.default_workspace_id = workspace.id
        raw = self._new_account_token(
            user, EMAIL_VERIFICATION, timedelta(hours=self._settings.email_verification_ttl_hours)
        )
        self._audit.record(
            "user.registered", actor_user_id=user.id, target_type="user", target_id=user.id
        )
        try:
            await self._session.commit()
        except IntegrityError:
            # Lost a race with a concurrent registration for the same email.
            await self._session.rollback()
            existing = await self._users.get_by_email(email)
            if existing is not None:
                await self._notify_existing_account(existing)
            return
        await self._email.send(
            emails.verification_email(
                user.email,
                user.display_name,
                self._settings.public_app_url,
                raw,
                self._settings.email_verification_ttl_hours,
            )
        )

    async def _notify_existing_account(self, user: User) -> None:
        if user.email_verified_at is None:
            await self._issue_verification(user)
        else:
            await self._email.send(
                emails.account_exists_email(
                    user.email, user.display_name, self._settings.public_app_url
                )
            )

    async def _issue_verification(self, user: User) -> None:
        now = utcnow()
        await self._account_tokens.invalidate_unused(user.id, EMAIL_VERIFICATION, now)
        raw = self._new_account_token(
            user, EMAIL_VERIFICATION, timedelta(hours=self._settings.email_verification_ttl_hours)
        )
        await self._session.commit()
        await self._email.send(
            emails.verification_email(
                user.email,
                user.display_name,
                self._settings.public_app_url,
                raw,
                self._settings.email_verification_ttl_hours,
            )
        )

    async def resend_verification(self, email: str) -> None:
        email = normalize_email(email)
        await self._limit_auth("verify-resend", email)
        user = await self._users.get_by_email(email)
        if user is not None and user.is_active and user.email_verified_at is None:
            await self._issue_verification(user)

    async def verify_email(self, token: str) -> None:
        await self._limit_auth("verify")
        record = await self._account_tokens.get_by_hash(hash_token(token), EMAIL_VERIFICATION)
        now = utcnow()
        if record is None or record.used_at is not None or record.expires_at <= now:
            raise InvalidTokenError("This verification link is invalid or has expired.")
        user = await self._users.get(record.user_id, for_update=True)
        if user is None:
            raise InvalidTokenError("This verification link is invalid or has expired.")
        record.used_at = now
        if user.email_verified_at is None:
            user.email_verified_at = now
            self._audit.record(
                "user.email_verified", actor_user_id=user.id, target_type="user", target_id=user.id
            )
        await self._session.commit()

    # ---------------------------------------------------------------- sessions

    async def login(self, email: str, password: str) -> AuthResult:
        s = self._settings
        email = normalize_email(email)
        await self._limiter.hit(
            "login:ip",
            self._meta.client_key,
            s.rate_limit_login_per_ip,
            s.rate_limit_login_window_seconds,
        )
        await self._limiter.hit(
            "login:email", email, s.rate_limit_login_per_email, s.rate_limit_login_window_seconds
        )
        invalid = AuthenticationError("Invalid email or password.", code="invalid_credentials")

        user = await self._users.get_by_email(email)
        if user is None:
            await self._hasher.verify_dummy(password)
            raise invalid
        if not await self._hasher.verify(user.password_hash, password) or not user.is_active:
            self._audit.record(
                "auth.login_failed", actor_user_id=user.id, target_type="user", target_id=user.id
            )
            await self._session.commit()
            raise invalid
        # Revealed only after a correct password, so this can't be used to enumerate.
        if user.email_verified_at is None:
            raise EmailNotVerifiedError("Please verify your email address before signing in.")

        if self._hasher.needs_rehash(user.password_hash):
            user.password_hash = await self._hasher.hash(password)
        user.last_login_at = utcnow()
        refresh, raw = self._start_session(user)
        self._audit.record(
            "auth.login_succeeded", actor_user_id=user.id, target_type="user", target_id=user.id
        )
        await self._session.commit()
        return self._result(user, refresh, raw)

    async def refresh(self, raw_token: str | None) -> AuthResult:
        s = self._settings
        await self._limiter.hit(
            "refresh:ip",
            self._meta.client_key,
            s.rate_limit_refresh_per_ip,
            s.rate_limit_refresh_window_seconds,
        )
        invalid = AuthenticationError(
            "Your session has expired. Please sign in again.", code="session_expired"
        )
        if not raw_token:
            raise invalid
        current = await self._refresh.get_by_hash(hash_token(raw_token), for_update=True)
        if current is None:
            raise invalid
        now = utcnow()

        if current.revoked_at is not None:
            if current.revoked_reason == "rotated" and now - current.revoked_at > ROTATION_GRACE:
                await self._refresh.revoke_family(current.family_id, "reuse_detected", now)
                self._audit.record(
                    "auth.refresh_token_reuse_detected",
                    actor_user_id=current.user_id,
                    target_type="session",
                    target_id=current.family_id,
                )
                await self._session.commit()
                logger.warning(
                    "Refresh token reuse detected; session revoked",
                    extra={"user_id": str(current.user_id)},
                )
            raise invalid
        if current.expires_at <= now:
            raise invalid

        user = await self._users.get(current.user_id)
        if user is None or not user.is_active:
            await self._refresh.revoke_family(current.family_id, "user_inactive", now)
            await self._session.commit()
            raise invalid

        replacement, raw = self._start_session(user, family_id=current.family_id)
        await self._session.flush()
        current.revoked_at = now
        current.revoked_reason = "rotated"
        current.replaced_by_id = replacement.id
        await self._session.commit()
        return self._result(user, replacement, raw)

    async def logout(self, raw_token: str | None) -> None:
        """Revoke this device's session. Idempotent; never reveals token validity."""
        if not raw_token:
            return
        token = await self._refresh.get_by_hash(hash_token(raw_token), for_update=True)
        if token is None:
            return
        await self._refresh.revoke_family(token.family_id, "logout", utcnow())
        self._audit.record(
            "auth.logout",
            actor_user_id=token.user_id,
            target_type="session",
            target_id=token.family_id,
        )
        await self._session.commit()

    async def logout_everywhere(self, user: User) -> None:
        locked = await self._users.require(user.id, for_update=True)
        await self._refresh.revoke_all_for_user(locked.id, "logout_all", utcnow())
        locked.auth_version += 1
        self._audit.record(
            "auth.logout_all", actor_user_id=locked.id, target_type="user", target_id=locked.id
        )
        await self._session.commit()

    async def authenticate(self, access_token: str) -> Principal:
        """Resolve the caller for an access token, enforcing revocation via auth_version."""
        claims = self._tokens.decode(access_token)
        user = await self._users.get(claims.user_id)
        if user is None or not user.is_active or user.auth_version != claims.auth_version:
            raise AuthenticationError("Your session is no longer valid. Please sign in again.")
        return Principal(user=user, mfa_verified_at=claims.mfa_verified_at)

    # ----------------------------------------------------------- password flows

    async def forgot_password(self, email: str) -> None:
        email = normalize_email(email)
        await self._limit_auth("password-forgot", email)
        user = await self._users.get_by_email(email)
        if user is None or not user.is_active:
            return
        now = utcnow()
        await self._account_tokens.invalidate_unused(user.id, PASSWORD_RESET, now)
        ttl = self._settings.password_reset_ttl_minutes
        raw = self._new_account_token(user, PASSWORD_RESET, timedelta(minutes=ttl))
        self._audit.record(
            "user.password_reset_requested",
            actor_user_id=user.id,
            target_type="user",
            target_id=user.id,
        )
        await self._session.commit()
        await self._email.send(
            emails.password_reset_email(
                user.email, user.display_name, self._settings.public_app_url, raw, ttl
            )
        )

    async def reset_password(self, token: str, new_password: str) -> None:
        await self._limit_auth("password-reset")
        record = await self._account_tokens.get_by_hash(hash_token(token), PASSWORD_RESET)
        now = utcnow()
        if record is None or record.used_at is not None or record.expires_at <= now:
            raise InvalidTokenError("This reset link is invalid or has expired.")
        user = await self._users.get(record.user_id, for_update=True)
        if user is None or not user.is_active:
            raise InvalidTokenError("This reset link is invalid or has expired.")
        validate_password(new_password, email=user.email)

        await self.apply_new_password(user, new_password, now)
        record.used_at = now
        # Receiving the reset email proves control of the address.
        if user.email_verified_at is None:
            user.email_verified_at = now
        self._audit.record(
            "user.password_reset", actor_user_id=user.id, target_type="user", target_id=user.id
        )
        await self._session.commit()
        await self._email.send(emails.password_changed_email(user.email, user.display_name))

    async def apply_new_password(self, user: User, new_password: str, now: datetime) -> None:
        """Set a new password and sign the user out everywhere (caller commits)."""
        user.password_hash = await self._hasher.hash(new_password)
        user.password_changed_at = now
        user.auth_version += 1
        await self._refresh.revoke_all_for_user(user.id, "password_changed", now)

    async def start_session_for(self, user: User) -> AuthResult:
        """Issue a fresh session (used after a password change on this device)."""
        refresh, raw = self._start_session(user)
        await self._session.commit()
        return self._result(user, refresh, raw)
