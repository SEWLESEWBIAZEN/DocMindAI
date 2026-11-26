"""Organization SSO (OIDC) and SCIM provisioning (ADR 0036).

The identity provider is mocked over HTTP but signs real RS256 ID tokens, so the
signature, issuer, audience, expiry and nonce checks all run.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

from documind.application.identity.common import RequestMeta
from documind.application.identity.sso_service import SsoService
from documind.container import build_container
from documind.infrastructure.sso.oidc import OidcClient
from tests.integration.conftest import PASSWORD, Api
from tests.integration.test_processing_pipeline import sql
from tests.integration.test_usage_and_quotas import assign

pytestmark = pytest.mark.integration

ISSUER = "https://localhost:9443/realms/acme"
DOMAIN = "acme.example.com"
ALICE = f"alice@{DOMAIN}"
DISCOVER = "/api/v1/auth/sso/discover"
CLIENT_ID = "documind-test"


class FakeIdp:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.email = ALICE
        self.nonce: str | None = None
        self.audience = CLIENT_ID
        self.sign_with_other = False
        self.seen_tokens: list[dict[str, Any]] = []

    def jwk(self) -> dict[str, Any]:
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {**public, "kid": "k1", "use": "sig", "alg": "RS256"}

    def id_token(self) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": self.audience,
            "sub": "idp-" + self.email,
            "email": self.email,
            "email_verified": True,
            "name": "Alice Example",
            "iat": now,
            "exp": now + 300,
            "nonce": self.nonce,
        }
        key = self.other_key if self.sign_with_other else self.key
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/auth",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/certs",
                },
            )
        if path.endswith("/certs"):
            return httpx.Response(200, json={"keys": [self.jwk()]})
        if path.endswith("/token"):
            form = parse_qs(request.content.decode())
            self.seen_tokens.append(form)
            assert form["client_secret"] == ["s3cret"]
            assert form["code_verifier"]
            return httpx.Response(200, json={"id_token": self.id_token(), "access_token": "x"})
        return httpx.Response(404)


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def sapi(make_api: Any, idp: FakeIdp) -> Api:
    oidc = OidcClient(
        allowed_private_hosts=frozenset({"localhost"}),
        http=httpx.AsyncClient(transport=httpx.MockTransport(idp.handler)),
    )
    api: Api = make_api({"oidc": oidc})
    return api


def org_with_sso(
    api: Api, idp: FakeIdp, *, enforced: bool = False
) -> tuple[str, dict[str, str], dict[str, str], str]:
    owner = api.signup("owner@acme.example.com")
    org = str(api.create_org(owner, "Acme")["id"])
    assign(api, org, "business")
    secret, _ = api.enable_mfa(owner)
    elevated = api.step_up(owner, secret)
    body = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": "s3cret",
        "domains": ["acme.example.com"],
        "default_role": "member",
        "enforced": enforced,
    }
    r = api.client.put(f"/api/v1/workspaces/{org}/sso", json=body, headers=elevated)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_verification"
    assert "s3cret" not in r.text
    return org, owner, elevated, secret


def approve(api: Api, org: str, *, yes: bool = True) -> None:
    async def go() -> None:
        c = build_container(api.settings)
        try:
            async with c.session_factory() as s:
                row_id = (
                    await s.execute(
                        text("SELECT id FROM sso_connections WHERE workspace_id = :w"),
                        {"w": uuid.UUID(org)},
                    )
                ).scalar_one()
                service = SsoService(
                    s,
                    api.settings,
                    c.oidc,
                    c.redis,
                    c.cipher,
                    c.password_hasher,
                    None,  # type: ignore[arg-type]
                    RequestMeta(),
                )
                await service.decide(row_id, approve=yes, operator="ops", note="DNS TXT checked")
        finally:
            await c.aclose()

    asyncio.run(go())


def sign_in(api: Api, idp: FakeIdp, email: str) -> Any:
    start = api.client.post("/api/v1/auth/sso/start", json={"email": email})
    assert start.status_code == 200, start.text
    q = parse_qs(urlparse(start.json()["url"]).query)
    assert q["code_challenge_method"] == ["S256"]
    assert q["login_hint"] == [email]
    idp.nonce = q["nonce"][0]
    return api.client.get(
        "/api/v1/auth/sso/callback",
        params={"state": q["state"][0], "code": "abc"},
        follow_redirects=False,
    )


def test_sso_needs_verification_then_signs_in_and_provisions(sapi: Api, idp: FakeIdp) -> None:
    org, owner, _, _ = org_with_sso(sapi, idp)
    # Pending: nobody can sign in with it yet.
    assert sapi.client.post(DISCOVER, json={"email": f"a@{DOMAIN}"}).json() == {
        "sso": False,
        "required": False,
    }
    assert (
        sapi.client.post("/api/v1/auth/sso/start", json={"email": f"a@{DOMAIN}"}).status_code == 404
    )
    approve(sapi, org)
    assert sapi.client.post(DISCOVER, json={"email": "A@Acme.Example.com"}).json()["sso"]

    back = sign_in(sapi, idp, ALICE)
    assert back.status_code == 303
    assert back.headers["location"] == f"https://app.test.example/sso/complete?workspace={org}"
    refreshed = sapi.client.post("/api/v1/auth/refresh", headers={"X-CSRF-Protection": "1"})
    assert refreshed.status_code == 200, refreshed.text
    alice = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    assert refreshed.json()["user"]["email"] == ALICE
    claims = jwt.decode(refreshed.json()["access_token"], options={"verify_signature": False})
    assert claims["sso"] == org  # survives the refresh
    # Provisioned on first sign-in: a member of the organization, with a personal workspace.
    spaces = {w["id"]: w for w in sapi.client.get("/api/v1/me/workspaces", headers=alice).json()}
    assert spaces[org]["role"] == "member"
    assert len(spaces) == 2
    assert sapi.client.get(f"/api/v1/workspaces/{org}/documents", headers=alice).status_code == 200
    actions = {r.action for r in sql(sapi, "SELECT action FROM audit_logs")}
    assert {
        "sso.configured",
        "sso.verified",
        "user.provisioned",
        "member.provisioned",
        "auth.login_sso",
    } <= actions
    # The client secret is only in the vault, encrypted.
    stored = sql(sapi, "SELECT ciphertext FROM credentials WHERE kind = 'sso_client_secret'")
    assert stored
    assert "s3cret" not in stored[0].ciphertext
    # Disabling the connection ends sessions started through it.
    approve(sapi, org, yes=False)
    again = sapi.client.post("/api/v1/auth/refresh", headers={"X-CSRF-Protection": "1"})
    assert again.status_code == 401
    del owner


def test_bad_tokens_wrong_domain_and_browser_binding(sapi: Api, idp: FakeIdp) -> None:
    org, _, _, _ = org_with_sso(sapi, idp)
    approve(sapi, org)
    idp.sign_with_other = True
    assert sign_in(sapi, idp, ALICE).headers["location"].endswith("sso_error=failed")
    idp.sign_with_other = False
    idp.audience = "someone-else"
    assert sign_in(sapi, idp, ALICE).headers["location"].endswith("sso_error=failed")
    idp.audience = CLIENT_ID
    idp.email = "mallory@evil.example.com"  # the IdP asserts an address outside the domains
    assert sign_in(sapi, idp, ALICE).headers["location"].endswith("sso_error=domain")
    idp.email = ALICE
    # Started in one browser, finished in another.
    start = sapi.client.post("/api/v1/auth/sso/start", json={"email": ALICE})
    q = parse_qs(urlparse(start.json()["url"]).query)
    idp.nonce = q["nonce"][0]
    sapi.client.cookies.clear()
    r = sapi.client.get(
        "/api/v1/auth/sso/callback",
        params={"state": q["state"][0], "code": "abc"},
        follow_redirects=False,
    )
    assert r.headers["location"].endswith("sso_error=browser")
    assert sql(sapi, "SELECT count(*) AS n FROM users WHERE email = :e", e=ALICE)[0].n == 0


def test_enforced_sso_blocks_passwords_and_validation(sapi: Api, idp: FakeIdp) -> None:
    org, owner, elevated, _ = org_with_sso(sapi, idp, enforced=True)
    # Before verification, nothing is enforced.
    assert sapi.login("owner@acme.example.com", PASSWORD).status_code == 200
    approve(sapi, org)
    blocked = sapi.login("owner@acme.example.com", PASSWORD)
    assert blocked.status_code == 401
    assert blocked.json()["code"] == "sso_required"
    # Someone at the same domain who isn't in the organization keeps their password.
    sapi.signup("bob@acme.example.com")
    assert sapi.login("bob@acme.example.com", PASSWORD).status_code == 200
    # Validation.
    bad = {
        "issuer": ISSUER,
        "client_id": "c",
        "client_secret": "s",
        "default_role": "member",
        "enforced": False,
    }
    for domains, code in (
        (["gmail.com"], "invalid_domains"),
        (["not a domain"], "invalid_domains"),
    ):
        r = sapi.client.put(
            f"/api/v1/workspaces/{org}/sso", json={**bad, "domains": domains}, headers=elevated
        )
        assert r.json()["code"] == code
    other = sapi.signup("o@other.example.com")
    oorg = str(sapi.create_org(other, "Other")["id"])
    s2, _ = sapi.enable_mfa(other)
    e2 = sapi.step_up(other, s2)
    r = sapi.client.put(
        f"/api/v1/workspaces/{oorg}/sso", json={**bad, "domains": ["other.example.com"]}, headers=e2
    )
    assert r.json()["code"] == "sso_plan_required"
    assign(sapi, oorg, "business")
    r = sapi.client.put(
        f"/api/v1/workspaces/{oorg}/sso", json={**bad, "domains": ["acme.example.com"]}, headers=e2
    )
    assert r.json()["code"] == "domain_taken"
    del owner


def test_scim_provisioning(sapi: Api, idp: FakeIdp) -> None:
    org, owner, elevated, secret = org_with_sso(sapi, idp)
    assert (
        sapi.client.post(f"/api/v1/workspaces/{org}/sso/scim-token", headers=elevated).json()[
            "code"
        ]
        == "sso_not_active"
    )
    approve(sapi, org)
    elevated = sapi.step_up(owner, secret)
    token = sapi.client.post(f"/api/v1/workspaces/{org}/sso/scim-token", headers=elevated).json()[
        "token"
    ]
    scim = {"Authorization": f"Bearer {token}"}
    assert (
        sapi.client.get(
            "/api/v1/scim/v2/Users", headers={"Authorization": "Bearer dmscim_nope"}
        ).status_code
        == 401
    )

    created = sapi.client.post(
        "/api/v1/scim/v2/Users",
        json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "carol@acme.example.com",
            "name": {"givenName": "Carol", "familyName": "C"},
            "active": True,
        },
        headers=scim,
    )
    assert created.status_code == 201, created.text
    uid = created.json()["id"]
    assert created.json()["active"] is True
    found = sapi.client.get(
        "/api/v1/scim/v2/Users",
        params={"filter": f'userName eq "carol@{DOMAIN}"'},
        headers=scim,
    ).json()
    assert found["totalResults"] == 1
    outside = sapi.client.post(
        "/api/v1/scim/v2/Users", json={"userName": "x@elsewhere.example.com"}, headers=scim
    )
    assert outside.status_code == 400
    members = {
        m["email"]
        for m in sapi.client.get(f"/api/v1/workspaces/{org}/members", headers=owner).json()
    }
    assert "carol@acme.example.com" in members

    patched = sapi.client.patch(
        f"/api/v1/scim/v2/Users/{uid}",
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "value": {"active": False}}],
        },
        headers=scim,
    )
    assert patched.json()["active"] is False
    members = {
        m["email"]
        for m in sapi.client.get(f"/api/v1/workspaces/{org}/members", headers=owner).json()
    }
    assert "carol@acme.example.com" not in members
    # The last owner can't be removed through SCIM.
    owner_id = sapi.client.get("/api/v1/me", headers=owner).json()["id"]
    assert sapi.client.delete(f"/api/v1/scim/v2/Users/{owner_id}", headers=scim).status_code == 409
