"""Short-lived JWT access tokens (HS256)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt

from documind.application.ports import AccessTokenClaims, IssuedAccessToken
from documind.domain.errors import AuthenticationError

_ALGORITHM = "HS256"
_TOKEN_TYPE = "access"  # noqa: S105 - JWT "typ" claim value, not a secret


class JwtAccessTokenService:
    def __init__(self, *, secret: str, issuer: str, audience: str, ttl_seconds: int) -> None:
        self._secret = secret
        self._issuer = issuer
        self._audience = audience
        self._ttl = ttl_seconds

    def issue(
        self,
        user_id: uuid.UUID,
        auth_version: int,
        *,
        mfa_verified_at: datetime | None = None,
        mfa_method: str | None = None,
        sso_workspace_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
    ) -> IssuedAccessToken:
        now = datetime.now(UTC)
        claims: dict[str, object] = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": str(user_id),
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(seconds=self._ttl),
            "jti": uuid.uuid4().hex,
            "typ": _TOKEN_TYPE,
            "ver": auth_version,
        }
        if sso_workspace_id is not None:
            claims["sso"] = str(sso_workspace_id)
        if session_id is not None:
            claims["sid"] = str(session_id)
        if mfa_verified_at is not None:
            claims["mfa"] = int(mfa_verified_at.timestamp())
            if mfa_method is not None:
                claims["mfm"] = mfa_method
        return IssuedAccessToken(
            token=jwt.encode(claims, self._secret, algorithm=_ALGORITHM), expires_in=self._ttl
        )

    def decode(self, token: str) -> AccessTokenClaims:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[_ALGORITHM],  # never accept "none" or asymmetric confusion
                audience=self._audience,
                issuer=self._issuer,
                leeway=10,
                options={"require": ["exp", "iat", "nbf", "sub", "iss", "aud", "typ", "ver"]},
            )
            if claims.get("typ") != _TOKEN_TYPE:
                raise AuthenticationError("Invalid token.")
            mfa = claims.get("mfa")
            return AccessTokenClaims(
                user_id=uuid.UUID(str(claims["sub"])),
                auth_version=int(claims["ver"]),
                mfa_verified_at=datetime.fromtimestamp(int(mfa), tz=UTC)
                if mfa is not None
                else None,
                mfa_method=str(claims["mfm"]) if mfa is not None and "mfm" in claims else None,
                sso_workspace_id=uuid.UUID(str(claims["sso"])) if "sso" in claims else None,
                session_id=uuid.UUID(str(claims["sid"])) if "sid" in claims else None,
            )
        except AuthenticationError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise AuthenticationError("Invalid or expired access token.") from exc
