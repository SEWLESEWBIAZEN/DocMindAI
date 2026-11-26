""" "Continue with Google" for every visitor (ADR 0040).

Google is faked over HTTP but signs real RS256 ID tokens, so signature, issuer,
audience, expiry and nonce checks all run.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
from pydantic import SecretStr

from documind.infrastructure.sso.oidc import GOOGLE_ISSUER, OidcClient
from tests.integration.conftest import PASSWORD, Api
from tests.integration.test_processing_pipeline import sql
from tests.integration.test_sso_api import ALICE, CLIENT_ID, FakeIdp, approve, org_with_sso

pytestmark = pytest.mark.integration

START = "/api/v1/auth/google/start"
CALLBACK = "/api/v1/auth/google/callback"


@pytest.fixture
def google(make_api: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Api, FakeIdp]:
    async def public(host: str, port: int, *, allowed: frozenset[str]) -> list[str]:
        del host, port, allowed
        return ["203.0.113.10"]

    monkeypatch.setattr("documind.infrastructure.sso.oidc.check_host", public)
    idp = FakeIdp(GOOGLE_ISSUER)
    idp.token_issuer = "accounts.google.com"
    oidc = OidcClient(http=httpx.AsyncClient(transport=httpx.MockTransport(idp.handler)))
    api: Api = make_api(
        {"oidc": oidc},
        google_oauth_client_id=CLIENT_ID,
        google_oauth_client_secret=SecretStr("s3cret"),
        google_signin_enabled=True,
    )
    return api, idp


def sign_in(api: Api, idp: FakeIdp, email: str, *, next_path: str | None = None) -> str:
    """Start, let "Google" answer for ``email``, follow the callback; returns the app path."""
    idp.email = email
    r = api.client.post(START, json={"next": next_path} if next_path else {})
    assert r.status_code == 200, r.text
    q = parse_qs(urlparse(r.json()["url"]).query)
    assert q["client_id"] == [CLIENT_ID]
    assert q["scope"] == ["openid email profile"]
    assert q["prompt"] == ["select_account"]
    idp.nonce = q["nonce"][0]
    back = api.client.get(
        CALLBACK, params={"state": q["state"][0], "code": "c0de"}, follow_redirects=False
    )
    assert back.status_code == 303
    location = back.headers["location"]
    assert location.startswith(api.settings.public_app_url)
    return location.removeprefix(api.settings.public_app_url.rstrip("/"))


def session_from_cookie(api: Api) -> dict[str, str]:
    r = api.refresh()
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_new_and_existing_people_sign_in_with_google(google: tuple[Api, FakeIdp]) -> None:
    api, idp = google
    assert api.client.get("/api/v1/auth/google").json() == {"enabled": True}

    # Someone new: an account and a personal workspace, verified by Google.
    path = sign_in(api, idp, "newbie@gmail.com", next_path="/w/abc/chat")
    assert path == "/sso/complete?next=%2Fw%2Fabc%2Fchat&welcome=1"
    me = api.client.get("/api/v1/me", headers=session_from_cookie(api)).json()
    assert me["email"] == "newbie@gmail.com"
    assert me["display_name"] == "Alice Example"
    assert me["email_verified"] is True
    spaces = sql(
        api,
        "SELECT w.kind FROM workspaces w JOIN users u ON u.id = w.personal_owner_id "
        "WHERE u.email = 'newbie@gmail.com'",
    )
    assert [s.kind for s in spaces] == ["personal"]
    audit = sql(api, "SELECT details FROM audit_logs WHERE action = 'user.registered'")
    assert audit[-1].details == {"via": "google"}

    # Someone with a password account: the same account, no second one.
    api.signup("bob@example.com")
    api.client.cookies.clear()
    assert sign_in(api, idp, "bob@example.com") == "/sso/complete"
    assert api.client.get("/api/v1/me", headers=session_from_cookie(api)).json()["email"] == (
        "bob@example.com"
    )
    assert sql(api, "SELECT count(*) AS n FROM users WHERE email = 'bob@example.com'")[0].n == 1
    # Unsafe "next" values are dropped.
    api.client.cookies.clear()
    assert sign_in(api, idp, "bob@example.com", next_path="//evil.example") == "/sso/complete"
    # Their password still works too.
    assert api.login("bob@example.com", PASSWORD).status_code == 200


def test_two_factor_still_applies(google: tuple[Api, FakeIdp]) -> None:
    api, idp = google
    carol = api.signup("carol@example.com")
    secret, _ = api.enable_mfa(carol)
    api.client.cookies.clear()
    path = sign_in(api, idp, "carol@example.com")
    assert path.startswith("/login#")
    fragment = parse_qs(path.split("#", 1)[1])
    assert "authenticator" in fragment["methods"][0].split(",")
    # No session until the second step.
    assert api.refresh().status_code == 401
    done = api.client.post(
        "/api/v1/auth/login/mfa",
        json={"challenge_token": fragment["mfa"][0], "code": api.totp(secret)},
    )
    assert done.status_code == 200, done.text
    actions = [
        r.details
        for r in sql(api, "SELECT details FROM audit_logs WHERE action = 'auth.login_mfa_required'")
    ]
    assert {"via": "google"} in actions


def test_sso_organizations_and_bad_callbacks(google: tuple[Api, FakeIdp]) -> None:
    api, idp = google
    org, _, _, _ = org_with_sso(api, idp)
    approve(api, org)
    api.client.cookies.clear()
    # A work address whose organization uses SSO goes to that SSO instead.
    path = sign_in(api, idp, ALICE)
    assert path.startswith("/login#sso=")
    assert unquote(path.split("=", 1)[1]) == ALICE
    assert sql(api, "SELECT count(*) AS n FROM users WHERE email = :e", e=ALICE)[0].n == 0

    # Callbacks bound to another browser, reused or cancelled are refused.
    r = api.client.post(START, json={})
    state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
    api.client.cookies.clear()
    back = api.client.get(CALLBACK, params={"state": state, "code": "x"}, follow_redirects=False)
    assert back.headers["location"].endswith("/login?google_error=browser")
    again = api.client.get(CALLBACK, params={"state": state, "code": "x"}, follow_redirects=False)
    assert again.headers["location"].endswith("/login?google_error=expired")
    r = api.client.post(START, json={})
    state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
    denied = api.client.get(
        CALLBACK, params={"state": state, "error": "access_denied"}, follow_redirects=False
    )
    assert denied.headers["location"].endswith("/login?google_error=denied")

    # A token signed with the wrong key is rejected.
    idp.sign_with_other = True
    assert sign_in(api, idp, "mallory@gmail.com") == "/login?google_error=failed"
    assert sql(api, "SELECT count(*) AS n FROM users WHERE email = 'mallory@gmail.com'")[0].n == 0


def test_off_unless_the_operator_turns_it_on(make_api: Any) -> None:
    api: Api = make_api()
    assert api.client.get("/api/v1/auth/google").json() == {"enabled": False}
    r = api.client.post(START, json={})
    assert r.status_code == 404
    assert r.json()["code"] == "google_signin_disabled"
