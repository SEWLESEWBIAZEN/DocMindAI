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
* Users with 2FA sign in in two steps (ADR 0026): a correct password yields a
  short-lived, single-use challenge, and only a valid code for that challenge
  starts a session.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity import emails
from documind.application.identity.common import AuditRecorder, Principal, RequestMeta, utcnow
from documind.application.identity.mfa_reset import MfaResetService
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
MFA_LOGIN = "mfa_login"
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


@dataclass(frozen=True, slots=True)
class MfaChallenge:
    """The password was right; a second factor is needed to finish signing in."""

    challenge_token: str
    expires_in: int
    # What the user can use: "authenticator", "email", "recovery_code" (ADR 0028).
    methods: tuple[str, ...] = ()


class VerifiedFactor(Protocol):
    @property
    def at(self) -> datetime: ...
    @property
    def method(self) -> str: ...


class SecondFactor(Protocol):
    async def verify(
        self,
        user: User,
        code: str | None,
        recovery_code: str | None,
        email_code: str | None = None,
        passkey: dict[str, Any] | None = None,
    ) -> VerifiedFactor:
        """Check a passkey, TOTP, recovery or email code; returns when and how."""
        ...

    async def send_email_code(self, user: User) -> None: ...

    async def passkey_options(self, user: User) -> dict[str, Any]: ...


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
        self,
        user: User,
        family_id: uuid.UUID | None = None,
        sso_workspace_id: uuid.UUID | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[RefreshToken, str]:
        raw = generate_token()
        token = RefreshToken(
            id=uuid.uuid4(),
            user_id=user.id,
            family_id=family_id or uuid.uuid4(),
            token_hash=hash_token(raw),
            expires_at=expires_at
            or utcnow() + timedelta(days=self._settings.refresh_token_ttl_days),
            user_agent=(self._meta.user_agent or "")[:255] or None,
            ip_address=self._meta.ip_address,
            sso_workspace_id=sso_workspace_id,
        )
        self._refresh.add(token)
        return token, raw

    def _result(
        self,
        user: User,
        refresh: RefreshToken,
        raw_refresh: str,
        *,
        mfa_verified_at: datetime | None = None,
        mfa_method: str | None = None,
    ) -> AuthResult:
        access = self._tokens.issue(
            user.id,
            user.auth_version,
            mfa_verified_at=mfa_verified_at,
            mfa_method=mfa_method,
            sso_workspace_id=refresh.sso_workspace_id,
        )
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

    async def login(self, email: str, password: str) -> AuthResult | MfaChallenge:
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
        from documind.application.identity.sso_service import sso_required_for

        if await sso_required_for(self._session, email) is not None:
            raise AuthenticationError(
                "Your organization signs in with single sign-on.", code="sso_required"
            )

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
        if user.mfa_enabled and await MfaResetService(
            self._session, self._email, self._meta, delay_hours=s.mfa_reset_delay_hours
        ).apply_if_due(user):
            # An approved support reset (ADR 0030) just turned 2FA off: sign in normally.
            logger.info("2FA reset applied at sign-in", extra={"user_id": str(user.id)})
        if user.mfa_enabled:
            # Second step required: no session yet, only a short-lived challenge.
            await self._account_tokens.invalidate_unused(user.id, MFA_LOGIN, utcnow())
            ttl = s.mfa_login_challenge_seconds
            raw = self._new_account_token(user, MFA_LOGIN, timedelta(seconds=ttl))
            self._audit.record(
                "auth.login_mfa_required",
                actor_user_id=user.id,
                target_type="user",
                target_id=user.id,
            )
            await self._session.commit()
            return MfaChallenge(
                challenge_token=raw, expires_in=ttl, methods=tuple(user.mfa_methods)
            )
        user.last_login_at = utcnow()
        refresh, raw = self._start_session(user)
        self._audit.record(
            "auth.login_succeeded", actor_user_id=user.id, target_type="user", target_id=user.id
        )
        await self._session.commit()
        return self._result(user, refresh, raw)

    async def _challenge_user(self, digest: str) -> User:
        expired = AuthenticationError(
            "This sign-in attempt has expired. Enter your email and password again.",
            code="mfa_challenge_expired",
        )
        record = await self._account_tokens.get_by_hash(digest, MFA_LOGIN)
        if record is None or record.used_at is not None or record.expires_at <= utcnow():
            raise expired
        user = await self._users.get(record.user_id)
        if user is None or not user.is_active or not user.mfa_enabled:
            raise expired
        return user

    async def send_login_email_code(
        self, challenge_token: str, second_factor: SecondFactor
    ) -> None:
        """During sign-in: email a one-time code instead of using the authenticator (ADR 0027)."""
        s = self._settings
        await self._limiter.hit(
            "login:ip",
            self._meta.client_key,
            s.rate_limit_login_per_ip,
            s.rate_limit_login_window_seconds,
        )
        user = await self._challenge_user(hash_token(challenge_token))
        await self._session.commit()
        await second_factor.send_email_code(user)

    async def login_passkey_options(
        self, challenge_token: str, second_factor: SecondFactor
    ) -> dict[str, Any]:
        """During sign-in: WebAuthn options for the user's passkeys (ADR 0029)."""
        user = await self._challenge_user(hash_token(challenge_token))
        await self._session.commit()
        return await second_factor.passkey_options(user)

    async def request_mfa_reset(self, challenge_token: str, reason: str) -> None:
        """Lost every factor: ask support for a reset (the challenge proves the password)."""
        s = self._settings
        await self._limit_auth("mfa_reset")
        user = await self._challenge_user(hash_token(challenge_token))
        await self._session.commit()
        await MfaResetService(
            self._session, self._email, self._meta, delay_hours=s.mfa_reset_delay_hours
        ).request(user, reason)

    async def complete_mfa_login(
        self,
        challenge_token: str,
        code: str | None,
        recovery_code: str | None,
        second_factor: SecondFactor,
        email_code: str | None = None,
        passkey: dict[str, Any] | None = None,
    ) -> AuthResult:
        """Second sign-in step: exchange the challenge and a valid code for a session."""
        s = self._settings
        await self._limiter.hit(
            "login:ip",
            self._meta.client_key,
            s.rate_limit_login_per_ip,
            s.rate_limit_login_window_seconds,
        )
        expired = AuthenticationError(
            "This sign-in attempt has expired. Enter your email and password again.",
            code="mfa_challenge_expired",
        )
        digest = hash_token(challenge_token)
        user = await self._challenge_user(digest)
        await self._session.commit()  # release the row lock while the code is checked
        # Rate-limited per user, audited, and replay-safe (TOTP steps, single-use recovery codes).
        verified = await second_factor.verify(user, code, recovery_code, email_code, passkey)

        record = await self._account_tokens.get_by_hash(digest, MFA_LOGIN)
        now = utcnow()
        if record is None or record.used_at is not None or record.expires_at <= now:
            raise expired  # used concurrently by another request
        record.used_at = now
        user = await self._users.require(record.user_id, for_update=True)
        user.last_login_at = now
        refresh, raw = self._start_session(user)
        self._audit.record(
            "auth.login_succeeded",
            actor_user_id=user.id,
            target_type="user",
            target_id=user.id,
            details={"mfa": True},
        )
        await self._session.commit()
        return self._result(
            user, refresh, raw, mfa_verified_at=verified.at, mfa_method=verified.method
        )

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

        # SSO sessions don't slide: they end when the first one does, so people sign
        # in with their identity provider again and leavers are caught (ADR 0036).
        replacement, raw = self._start_session(
            user,
            family_id=current.family_id,
            sso_workspace_id=current.sso_workspace_id,
            expires_at=current.expires_at if current.sso_workspace_id else None,
        )
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
        return Principal(
            user=user,
            mfa_verified_at=claims.mfa_verified_at,
            mfa_method=claims.mfa_method,
            sso_workspace_id=claims.sso_workspace_id,
        )

    async def sso_session(self, user: User, workspace_id: uuid.UUID) -> AuthResult:
        """A session after signing in with an organization's SSO (ADR 0036)."""
        refresh, raw = self._start_session(
            user,
            sso_workspace_id=workspace_id,
            expires_at=utcnow() + timedelta(hours=self._settings.sso_session_hours),
        )
        self._audit.record(
            "auth.login_sso",
            actor_user_id=user.id,
            workspace_id=workspace_id,
            target_type="user",
            target_id=user.id,
        )
        await self._session.commit()
        return self._result(user, refresh, raw)

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
