"""OpenID Connect relying party: discovery, code exchange, ID token checks (ADR 0036)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from documind.infrastructure.network.ssrf import BlockedHostError, check_host

ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "ES256", "ES384"]


class OidcError(Exception):
    """The identity provider is unreachable, misconfigured, or returned an invalid token."""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True, slots=True)
class Identity:
    subject: str
    email: str
    name: str | None


async def _safe(url: str, allowed: frozenset[str]) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" and not (
        parts.scheme == "http" and (parts.hostname or "") in allowed
    ):
        raise OidcError("Identity provider URLs must use https.")
    if not parts.hostname:
        raise OidcError("Invalid identity provider URL.")
    try:
        await check_host(parts.hostname.lower(), parts.port or 443, allowed=allowed)
    except BlockedHostError as exc:
        raise OidcError(str(exc)) from exc


class OidcClient:
    def __init__(
        self,
        *,
        allowed_private_hosts: frozenset[str] = frozenset(),
        http: httpx.AsyncClient | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._allowed = allowed_private_hosts
        self._http = http or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)

    async def _json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        await _safe(url, self._allowed)
        try:
            method = "POST" if "data" in kwargs else "GET"
            r = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise OidcError("The identity provider is unreachable.") from exc
        if r.status_code >= 400:
            raise OidcError(f"The identity provider returned {r.status_code}.")
        try:
            body: dict[str, Any] = r.json()
        except json.JSONDecodeError as exc:
            raise OidcError("The identity provider returned invalid JSON.") from exc
        return body

    async def discover(self, issuer: str) -> ProviderConfig:
        issuer = issuer.rstrip("/")
        meta = await self._json(f"{issuer}/.well-known/openid-configuration")
        if str(meta.get("issuer", "")).rstrip("/") != issuer:
            raise OidcError("The discovery document is for a different issuer.")
        try:
            return ProviderConfig(
                issuer=str(meta["issuer"]),
                authorization_endpoint=str(meta["authorization_endpoint"]),
                token_endpoint=str(meta["token_endpoint"]),
                jwks_uri=str(meta["jwks_uri"]),
            )
        except KeyError as exc:
            raise OidcError(f"The discovery document has no {exc.args[0]}.") from exc

    @staticmethod
    def authorize_url(
        config: ProviderConfig,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
        login_hint: str | None = None,
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if login_hint:
            params["login_hint"] = login_hint
        sep = "&" if "?" in config.authorization_endpoint else "?"
        return f"{config.authorization_endpoint}{sep}{urlencode(params)}"

    async def identity(
        self,
        config: ProviderConfig,
        *,
        client_id: str,
        client_secret: str,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        nonce: str,
    ) -> Identity:
        tokens = await self._json(
            config.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": code_verifier,
            },
        )
        raw = tokens.get("id_token")
        if not isinstance(raw, str):
            raise OidcError("No ID token was returned.")
        jwks = await self._json(config.jwks_uri)
        try:
            header = jwt.get_unverified_header(raw)
            keys = jwt.PyJWKSet.from_dict(jwks)
            key = next((k for k in keys.keys if header.get("kid") in (None, k.key_id)), None)
            if key is None:
                raise OidcError("The ID token's signing key is unknown.")
            claims: dict[str, Any] = jwt.decode(
                raw,
                key=key.key,
                algorithms=ALGORITHMS,
                audience=client_id,
                issuer=config.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                leeway=60,
            )
        except (jwt.PyJWTError, jwt.PyJWKSetError) as exc:
            raise OidcError(f"The ID token is invalid: {exc}") from exc
        if claims.get("nonce") != nonce:
            raise OidcError("The ID token's nonce doesn't match.")
        email = claims.get("email")
        if not isinstance(email, str) or "@" not in email:
            raise OidcError("The identity provider didn't send an email address.")
        if claims.get("email_verified") is False:
            raise OidcError("The identity provider says the email isn't verified.")
        name = claims.get("name")
        return Identity(
            str(claims["sub"]), email.strip().lower(), name if isinstance(name, str) else None
        )
