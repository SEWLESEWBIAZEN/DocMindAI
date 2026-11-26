""" "Continue with Google" for every visitor (ADR 0040).

One Google OAuth client for the whole server, set by the operator. It's the
organization-SSO flow (ADR 0036) with Google as the only provider:

* The callback is bound to the browser that started it (single-use state in
  Redis plus an HttpOnly nonce cookie), with PKCE and a nonce in the ID token.
* The ID token is verified with Google's keys; the email must be verified.
* An address whose organization has active single sign-on goes to that
  organization's SSO instead, so Google can't bypass it.
* Existing accounts are matched by email. New people get an account with a
  personal workspace, like a password sign-up, already verified by Google.
* 2FA still applies: people who turned it on get the usual second step.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity.auth_service import AuthResult, AuthService, MfaChallenge
from documind.application.identity.common import AuditRecorder, RequestMeta, utcnow
from documind.application.identity.sso_service import SsoService
from documind.application.ports import RateLimiter
from documind.core.config import Settings
from documind.domain.errors import NotFoundError
from documind.infrastructure.database.repositories.identity import (
    AuditLogRepository,
    UserRepository,
)
from documind.infrastructure.sso.oidc import GOOGLE_ISSUER, OidcClient, OidcError

STATE_TTL_SECONDS = 600
NONCE_COOKIE = "documind_google_nonce"
CALLBACK_PATH = "/api/v1/auth/google/callback"


def _key(state: str) -> str:
    return f"google:signin:{hashlib.sha256(state.encode()).hexdigest()}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def safe_next(value: str | None) -> str | None:
    """Only same-site paths: never ``//evil.example`` or a full URL."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None
    return value[:500]


@dataclass(frozen=True, slots=True)
class GoogleStart:
    url: str
    nonce: str


@dataclass(frozen=True, slots=True)
class GoogleFinish:
    path: str
    session: AuthResult | None = None


class GoogleSignInService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        oidc: OidcClient,
        redis: Redis,
        limiter: RateLimiter,
        auth: AuthService,
        sso: SsoService,
        meta: RequestMeta,
    ) -> None:
        self._s = session
        self._settings = settings
        self._oidc = oidc
        self._redis = redis
        self._limiter = limiter
        self._auth = auth
        self._sso = sso
        self._meta = meta
        self._users = UserRepository(session)
        self._audit = AuditRecorder(AuditLogRepository(session), meta)

    def redirect_uri(self) -> str:
        return f"{self._settings.public_app_url.rstrip('/')}{CALLBACK_PATH}"

    def _require(self) -> None:
        if not self._settings.google_signin_available:
            raise NotFoundError(
                "Signing in with Google isn't available here.", code="google_signin_disabled"
            )

    async def start(self, next_path: str | None) -> GoogleStart:
        self._require()
        s = self._settings
        await self._limiter.hit(
            "google:ip",
            self._meta.client_key,
            s.rate_limit_login_per_ip,
            s.rate_limit_login_window_seconds,
        )
        try:
            config = await self._oidc.discover(GOOGLE_ISSUER)
        except OidcError as exc:
            raise NotFoundError(
                "Google sign-in is unavailable right now. Try again, or use your password.",
                code="google_unavailable",
            ) from exc
        state, nonce, browser = (secrets.token_urlsafe(32) for _ in range(3))
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        await self._redis.set(
            _key(state),
            json.dumps(
                {
                    "verifier": verifier,
                    "nonce": nonce,
                    "browser": _digest(browser),
                    "next": safe_next(next_path),
                }
            ),
            ex=STATE_TTL_SECONDS,
        )
        url = self._oidc.authorize_url(
            config,
            client_id=s.google_oauth_client_id or "",
            redirect_uri=self.redirect_uri(),
            state=state,
            nonce=nonce,
            code_challenge=challenge,
        )
        # Let people pick which Google account, instead of silently reusing the last one.
        return GoogleStart(f"{url}&prompt=select_account", browser)

    async def finish(
        self, *, state: str | None, code: str | None, error: str | None, browser: str | None
    ) -> GoogleFinish:
        if not self._settings.google_signin_available:
            return GoogleFinish("/login?google_error=unavailable")
        raw = await self._redis.getdel(_key(state)) if state else None
        if raw is None:
            return GoogleFinish("/login?google_error=expired")
        pending: dict[str, Any] = json.loads(raw)
        if not browser or not hmac.compare_digest(_digest(browser), pending["browser"]):
            return GoogleFinish("/login?google_error=browser")
        if error or not code:
            return GoogleFinish("/login?google_error=denied")
        s = self._settings
        try:
            config = await self._oidc.discover(GOOGLE_ISSUER)
            identity = await self._oidc.identity(
                config,
                client_id=s.google_oauth_client_id or "",
                client_secret=(
                    s.google_oauth_client_secret.get_secret_value()
                    if s.google_oauth_client_secret
                    else ""
                ),
                code=code,
                code_verifier=pending["verifier"],
                redirect_uri=self.redirect_uri(),
                nonce=pending["nonce"],
            )
        except OidcError:
            self._audit.record("auth.login_google_failed", actor_user_id=None)
            await self._s.commit()
            return GoogleFinish("/login?google_error=failed")

        email = identity.email
        # An organization with single sign-on signs its people in itself.
        if await self._sso.for_email(email) is not None:
            return GoogleFinish(f"/login#sso={quote(email)}")

        user = await self._users.get_by_email(email)
        created = False
        if user is None:
            await self._limiter.hit(
                "register:ip",
                self._meta.client_key,
                s.rate_limit_login_per_ip,
                s.rate_limit_login_window_seconds,
            )
            user = await self._auth.register_verified(email, identity.name or "", via="google")
            try:
                await self._s.commit()
            except IntegrityError:  # registered meanwhile, e.g. a double click
                await self._s.rollback()
                user = await self._users.get_by_email(email)
                if user is None:
                    return GoogleFinish("/login?google_error=failed")
            else:
                created = True
        if not user.is_active:
            return GoogleFinish("/login?google_error=inactive")
        if user.email_verified_at is None:
            user.email_verified_at = utcnow()  # Google vouches for the address
        result = await self._auth.continue_sign_in(user, via="google")
        after = pending.get("next")
        if isinstance(result, MfaChallenge):
            fragment = urlencode(
                {
                    "mfa": result.challenge_token,
                    "methods": ",".join(result.methods),
                    **({"next": after} if after else {}),
                }
            )
            return GoogleFinish(f"/login#{fragment}")
        query = urlencode(
            {**({"next": after} if after else {}), **({"welcome": "1"} if created else {})}
        )
        return GoogleFinish(f"/sso/complete{'?' + query if query else ''}", result)
