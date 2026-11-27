"""Sign-in with 2FA (ADR 0026): password, then a single-use challenge and a code."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.integration.conftest import PASSWORD, Api
from tests.integration.test_processing_pipeline import sql

pytestmark = pytest.mark.integration


def _user_with_mfa(api: Api) -> tuple[str, str, list[str]]:
    email = f"mfa-{uuid.uuid4().hex[:8]}@example.com"
    headers = api.signup(email)
    secret, codes = api.enable_mfa(headers)
    return email, secret, codes


def _finish(api: Api, challenge: str, **factor: str) -> Any:
    return api.client.post("/api/v1/auth/login/mfa", json={"challenge_token": challenge, **factor})


def test_accounts_without_2fa_sign_in_with_a_password(api: Api) -> None:
    email = f"plain-{uuid.uuid4().hex[:8]}@example.com"
    api.signup(email)
    response = api.login(email)
    assert response.status_code == 200
    assert response.json()["access_token"]
    assert "mfa_required" not in response.json()


def test_2fa_accounts_need_a_code_to_sign_in(api: Api) -> None:
    email, secret, _ = _user_with_mfa(api)
    api.client.cookies.clear()

    first = api.login(email)
    assert first.status_code == 200
    body = first.json()
    assert body["mfa_required"] is True
    assert "access_token" not in body
    assert api.settings.refresh_cookie_name not in first.cookies  # no session yet
    assert first.headers["Cache-Control"] == "no-store"
    challenge = body["challenge_token"]

    wrong = _finish(api, challenge, code="000000")
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "invalid_mfa_code"

    done = _finish(api, challenge, code=api.totp(secret))
    assert done.status_code == 200, done.text
    token = done.json()["access_token"]
    assert api.settings.refresh_cookie_name in done.cookies
    me = api.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == email

    # The challenge is single use, and a refreshed session keeps working.
    reused = _finish(api, challenge, code=api.totp(secret))
    assert reused.status_code == 401
    assert reused.json()["code"] == "mfa_challenge_expired"
    refreshed = api.client.post("/api/v1/auth/refresh", headers={"X-CSRF-Protection": "1"})
    assert refreshed.status_code == 200, refreshed.text

    actions = [
        r.action
        for r in sql(
            api,
            "SELECT a.action FROM audit_logs a JOIN users u ON u.id = a.actor_user_id "
            "WHERE u.email = :e ORDER BY a.created_at",
            e=email,
        )
    ]
    assert "auth.login_mfa_required" in actions
    assert "auth.mfa_failed" in actions
    assert actions.count("auth.login_succeeded") == 2  # sign-up sign-in, then this one


def test_a_recovery_code_finishes_sign_in_once(api: Api) -> None:
    email, _, codes = _user_with_mfa(api)
    challenge = api.login(email).json()["challenge_token"]
    assert _finish(api, challenge, recovery_code=codes[0]).status_code == 200
    assert any("recovery code" in m.subject.lower() for m in api.mail.sent if m.to == email)
    again = api.login(email).json()["challenge_token"]
    assert _finish(api, again, recovery_code=codes[0]).status_code == 401  # already used


def test_challenges_expire_and_are_replaced(api: Api) -> None:
    email, secret, _ = _user_with_mfa(api)
    old = api.login(email).json()["challenge_token"]
    new = api.login(email).json()["challenge_token"]
    # A new password step invalidates the previous challenge.
    assert _finish(api, old, code=api.totp(secret)).json()["code"] == "mfa_challenge_expired"
    sql(
        api,
        "UPDATE account_tokens SET expires_at = now() - interval '1 second' "
        "WHERE purpose = 'mfa_login' AND used_at IS NULL",
    )
    assert _finish(api, new, code=api.totp(secret)).json()["code"] == "mfa_challenge_expired"
    assert _finish(api, "x" * 43, code="123456").status_code == 401
    # A wrong password never reveals that the account has 2FA.
    wrong = api.login(email, password=PASSWORD + "x")
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "invalid_credentials"


def test_code_guessing_is_rate_limited(make_api: Any) -> None:
    api: Api = make_api(rate_limit_mfa_per_user=5)
    email, secret, _ = _user_with_mfa(api)  # setup and confirm count towards the limit too
    challenge = api.login(email).json()["challenge_token"]
    statuses = [_finish(api, challenge, code="000000").status_code for _ in range(5)]
    assert statuses[0] == 401
    assert statuses[-1] == 429
    assert set(statuses) == {401, 429}
    # The limit is per user, so even the right code waits for the window to pass.
    assert _finish(api, challenge, code=api.totp(secret)).status_code == 429


def test_turning_2fa_off_restores_password_sign_in(api: Api) -> None:
    email, secret, _ = _user_with_mfa(api)
    challenge = api.login(email).json()["challenge_token"]
    token = _finish(api, challenge, code=api.totp(secret)).json()["access_token"]
    off = api.client.post(
        "/api/v1/me/mfa/disable",
        json={"password": PASSWORD, "code": api.totp(secret)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert off.status_code in (200, 204), off.text
    assert "access_token" in api.login(email).json()


def test_rotating_the_mfa_key_keeps_everyone_signed_in(make_api: Any) -> None:
    """Production readiness F4: a new MFA_ENCRYPTION_KEY, a rewrap, then the old key retired."""
    import asyncio
    import base64
    import os

    from pydantic import SecretStr

    from documind.container import build_container
    from documind.ops.mfa_keys import rewrap
    from tests.integration.test_processing_pipeline import sql

    old_api: Api = make_api()
    email, secret, codes = _user_with_mfa(old_api)
    before = sql(
        old_api, "SELECT totp_secret_encrypted AS enc FROM users WHERE email = :e", e=email
    )[0].enc
    old_key = old_api.settings.mfa_encryption_key
    new_key = SecretStr(base64.urlsafe_b64encode(os.urandom(32)).decode())

    def sign_in(api: Api, **factor: str) -> int:
        challenge = api.login(email).json()["challenge_token"]
        return int(_finish(api, challenge, **factor).status_code)

    # 1. New key, old one kept as previous: everything still works.
    rotated: Api = make_api(
        _skip_reset=True, mfa_encryption_key=new_key, mfa_encryption_keys_previous=old_key
    )
    assert sign_in(rotated, code=rotated.totp(secret)) == 200
    assert sign_in(rotated, recovery_code=codes[0]) == 200

    def run_rewrap(api: Api, dry_run: bool) -> dict[str, object]:
        async def go() -> dict[str, object]:
            c = build_container(api.settings)
            try:
                return (await rewrap(c.session_factory, c.secret_box, dry_run=dry_run)).as_dict()
            finally:
                await c.aclose()

        return asyncio.run(go())

    # 2. Dry run counts without changing anything; the real run re-encrypts.
    dry = run_rewrap(rotated, dry_run=True)
    assert dry["rewrapped"] == 1
    assert (
        sql(rotated, "SELECT totp_secret_encrypted AS enc FROM users WHERE email = :e", e=email)[
            0
        ].enc
        == before
    )
    done = run_rewrap(rotated, dry_run=False)
    assert (done["rewrapped"], done["undecryptable_user_ids"]) == (1, [])
    after = sql(
        rotated, "SELECT totp_secret_encrypted AS enc FROM users WHERE email = :e", e=email
    )[0].enc
    assert after.startswith(f"v2:{done['current_key_id']}:")
    assert secret not in after
    assert run_rewrap(rotated, dry_run=False)["already_current"] == 1  # idempotent

    # 3. Old key retired: authenticator codes still work. Recovery codes issued under
    #    the old key don't (keyed hashes can't be re-keyed): documented in the runbook.
    retired: Api = make_api(_skip_reset=True, mfa_encryption_key=new_key)
    assert sign_in(retired, code=retired.totp(secret)) == 200
    assert sign_in(retired, recovery_code=codes[1]) == 401

    # A key removed before rewrapping is a clear error, not a crash.
    lost: Api = make_api(
        _skip_reset=True,
        mfa_encryption_key=SecretStr(base64.urlsafe_b64encode(os.urandom(32)).decode()),
    )
    challenge = lost.login(email).json()["challenge_token"]
    r = _finish(lost, challenge, code=lost.totp(secret))
    assert r.status_code == 503
    assert r.json()["code"] == "mfa_unavailable"
    assert run_rewrap(lost, dry_run=True)["undecryptable_user_ids"] != []
