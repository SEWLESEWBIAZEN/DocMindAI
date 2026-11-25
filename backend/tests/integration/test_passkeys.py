"""Passkeys (WebAuthn) as a second factor (ADR 0029)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.integration.conftest import PASSWORD, Api
from tests.integration.soft_authenticator import SoftAuthenticator
from tests.integration.test_mfa_email_method import email_step_up
from tests.integration.test_processing_pipeline import sql
from tests.integration.test_usage_and_quotas import assign

pytestmark = pytest.mark.integration


def _email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def device(api: Api) -> SoftAuthenticator:
    origin = api.settings.public_app_url.rstrip("/")
    return SoftAuthenticator(origin.split("://", 1)[1].split("/")[0], origin)


def add_passkey(
    api: Api, headers: dict[str, str], key: SoftAuthenticator, name: str = "Laptop"
) -> Any:
    options = api.client.post(
        "/api/v1/me/mfa/passkeys/options", json={"password": PASSWORD}, headers=headers
    )
    assert options.status_code == 200, options.text
    body = options.json()
    assert body["rp"]["id"] == key.rp_id
    return api.client.post(
        "/api/v1/me/mfa/passkeys",
        json={"name": name, "credential": key.create(body)},
        headers=headers,
    )


def sign_in_with_passkey(api: Api, email: str, key: SoftAuthenticator) -> Any:
    challenge = api.login(email).json()
    assert "passkey" in challenge["methods"]
    options = api.client.post(
        "/api/v1/auth/login/mfa/passkey-options",
        json={"challenge_token": challenge["challenge_token"]},
    ).json()
    return api.client.post(
        "/api/v1/auth/login/mfa",
        json={"challenge_token": challenge["challenge_token"], "passkey": key.get(options)},
    )


def test_a_passkey_turns_2fa_on_and_signs_in(api: Api) -> None:
    email = _email("pk")
    headers = api.signup(email)
    key = device(api)
    added = add_passkey(api, headers, key)
    assert added.status_code == 201, added.text
    assert len(added.json()["recovery_codes"]) == 10  # first method: 2FA is now on
    assert api.client.get("/api/v1/me/mfa", headers=headers).json()["method"] == "passkey"
    listed = api.client.get("/api/v1/me/mfa/passkeys", headers=headers).json()
    assert [p["name"] for p in listed] == ["Laptop"]
    stored = sql(api, "SELECT public_key FROM webauthn_credentials")
    assert stored
    assert all(len(r.public_key) > 60 for r in stored)

    assert api.login(email).json()["methods"] == ["passkey", "email", "recovery_code"]
    done = sign_in_with_passkey(api, email, key)
    assert done.status_code == 200, done.text
    assert api.client.get("/api/v1/me/mfa/passkeys", headers=headers).json()[0]["last_used_at"]


def test_passkey_responses_are_bound_to_their_challenge_and_origin(api: Api) -> None:
    email = _email("bind")
    headers = api.signup(email)
    key = device(api)
    add_passkey(api, headers, key)
    challenge = api.login(email).json()["challenge_token"]
    options = api.client.post(
        "/api/v1/auth/login/mfa/passkey-options", json={"challenge_token": challenge}
    ).json()
    assertion = key.get(options)
    finish = {"challenge_token": challenge, "passkey": assertion}
    assert api.client.post("/api/v1/auth/login/mfa", json=finish).status_code == 200
    # Replaying the same signed response fails: its challenge was used.
    again = api.login(email).json()["challenge_token"]
    replay = api.client.post(
        "/api/v1/auth/login/mfa", json={"challenge_token": again, "passkey": assertion}
    )
    assert replay.status_code == 401
    assert replay.json()["code"] == "invalid_passkey"
    # A phishing site's origin is refused.
    options = api.client.post(
        "/api/v1/auth/login/mfa/passkey-options", json={"challenge_token": again}
    ).json()
    phished = key.get(options, origin="https://app.test.example.evil.test")
    assert (
        api.client.post(
            "/api/v1/auth/login/mfa", json={"challenge_token": again, "passkey": phished}
        ).status_code
        == 401
    )


def test_a_cloned_authenticator_is_rejected(api: Api) -> None:
    email = _email("clone")
    headers = api.signup(email)
    key = device(api)
    add_passkey(api, headers, key)
    assert sign_in_with_passkey(api, email, key).status_code == 200
    assert sign_in_with_passkey(api, email, key).status_code == 200
    key.sign_count = 0  # a copy of the key with an older counter
    assert sign_in_with_passkey(api, email, key).status_code == 401


def test_passkeys_count_where_email_codes_are_refused(api: Api) -> None:
    owner_email = _email("owner")
    owner = api.signup(owner_email)
    org = str(api.create_org(owner, "Passkey Org")["id"])
    assign(api, org, "pro")
    key = device(api)
    add_passkey(api, owner, key)

    def step_up() -> dict[str, str]:
        options = api.client.post("/api/v1/auth/mfa/passkey-options", headers=owner).json()
        verified = api.client.post(
            "/api/v1/auth/mfa/verify", json={"passkey": key.get(options)}, headers=owner
        )
        assert verified.status_code == 200, verified.text
        return {"Authorization": f"Bearer {verified.json()['access_token']}"}

    url = f"/api/v1/workspaces/{org}/mfa-requirement"
    assert (
        api.client.put(url, json={"allow_email_codes": False}, headers=step_up()).status_code == 200
    )
    # Passkey step-ups still count there; email step-ups don't.
    assert api.client.put(url, json={"required": True}, headers=step_up()).status_code == 200
    assert (
        api.client.put(
            url, json={"required": False}, headers=email_step_up(api, owner, owner_email)
        ).json()["code"]
        == "mfa_step_up_required"
    )


def test_removing_passkeys(api: Api) -> None:
    email = _email("rm")
    headers = api.signup(email)
    key = device(api)
    added = add_passkey(api, headers, key).json()["passkey"]
    url = f"/api/v1/me/mfa/passkeys/{added['id']}/remove"
    only = api.client.post(url, json={"password": PASSWORD}, headers=headers)
    assert only.status_code == 409
    assert only.json()["code"] == "last_mfa_method"
    # With an authenticator as well, the passkey can go.
    setup = api.client.post(
        "/api/v1/me/mfa/totp/setup", json={"password": PASSWORD}, headers=headers
    ).json()
    api.client.post(
        "/api/v1/me/mfa/totp/confirm", json={"code": api.totp(setup["secret"])}, headers=headers
    )
    assert api.client.post(url, json={"password": "wrong"}, headers=headers).status_code == 401
    assert api.client.post(url, json={"password": PASSWORD}, headers=headers).status_code == 204
    assert api.client.get("/api/v1/me/mfa", headers=headers).json()["method"] == "authenticator"
    assert any("passkey was removed" in m.subject for m in api.mail.to(email))


def test_registration_needs_a_fresh_challenge(api: Api) -> None:
    email = _email("reg")
    headers = api.signup(email)
    key = device(api)
    options = api.client.post(
        "/api/v1/me/mfa/passkeys/options", json={"password": PASSWORD}, headers=headers
    ).json()
    created = key.create(options)
    first = api.client.post(
        "/api/v1/me/mfa/passkeys", json={"name": "A", "credential": created}, headers=headers
    )
    assert first.status_code == 201
    second = api.client.post(
        "/api/v1/me/mfa/passkeys", json={"name": "B", "credential": created}, headers=headers
    )
    assert second.status_code == 401
    assert second.json()["code"] == "passkey_challenge_expired"
