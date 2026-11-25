"""One-time codes by email (ADR 0027): a fallback second factor, also for a lost authenticator."""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest

from tests.integration.conftest import PASSWORD, Api
from tests.integration.test_processing_pipeline import sql

pytestmark = pytest.mark.integration


def _account(api: Api) -> tuple[str, dict[str, str], str]:
    email = f"otp-{uuid.uuid4().hex[:8]}@example.com"
    headers = api.signup(email)
    secret, _ = api.enable_mfa(headers)
    return email, headers, secret


def _code(api: Api, email: str) -> str:
    """The latest one-time code emailed to ``email``."""
    for message in reversed(api.mail.to(email)):
        match = re.search(r"one-time code is (\d{3}) (\d{3})", message.text)
        if match:
            return match.group(1) + match.group(2)
    raise AssertionError(f"no code emailed to {email}")


def _challenge(api: Api, email: str) -> str:
    return str(api.login(email).json()["challenge_token"])


def _send_for_sign_in(api: Api, challenge: str) -> Any:
    return api.client.post("/api/v1/auth/login/mfa/email-code", json={"challenge_token": challenge})


def _finish(api: Api, challenge: str, **factor: str) -> Any:
    return api.client.post("/api/v1/auth/login/mfa", json={"challenge_token": challenge, **factor})


def test_signing_in_with_an_email_code(api: Api) -> None:
    email, _, _ = _account(api)
    challenge = _challenge(api, email)
    sent = _send_for_sign_in(api, challenge)
    assert sent.status_code == 202
    code = _code(api, email)
    assert code not in sent.text  # only ever in the email

    assert _finish(api, challenge, email_code="000000").status_code == 401
    done = _finish(api, challenge, email_code=f"{code[:3]} {code[3:]}")  # spaces are fine
    assert done.status_code == 200, done.text
    assert any(m.subject == "An email code was used on your account" for m in api.mail.to(email))

    # Single use, and only the latest code counts.
    again = _challenge(api, email)
    assert _finish(api, again, email_code=code).status_code == 401
    _send_for_sign_in(api, again)
    first = _code(api, email)
    _send_for_sign_in(api, again)
    second = _code(api, email)
    if first != second:
        assert _finish(api, again, email_code=first).status_code == 401
    assert _finish(api, again, email_code=second).status_code == 200
    stored = sql(api, "SELECT token_hash FROM account_tokens WHERE purpose = 'mfa_email'")
    assert all(second not in r.token_hash for r in stored)  # kept as a keyed hash


def test_codes_expire(api: Api) -> None:
    email, _, _ = _account(api)
    challenge = _challenge(api, email)
    _send_for_sign_in(api, challenge)
    sql(
        api,
        "UPDATE account_tokens SET expires_at = now() - interval '1 second' "
        "WHERE purpose = 'mfa_email'",
    )
    assert _finish(api, challenge, email_code=_code(api, email)).status_code == 401


def test_recovering_from_a_lost_authenticator(api: Api) -> None:
    email, _, _ = _account(api)
    # 1. Sign in with an email code instead of the lost app.
    challenge = _challenge(api, email)
    _send_for_sign_in(api, challenge)
    token = _finish(api, challenge, email_code=_code(api, email)).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    # 2. Turn 2FA off with the password and a fresh email code.
    assert api.client.post("/api/v1/auth/mfa/email-code", headers=headers).status_code == 202
    off = api.client.post(
        "/api/v1/me/mfa/disable",
        json={"password": PASSWORD, "email_code": _code(api, email)},
        headers=headers,
    )
    assert off.status_code == 204, off.text
    assert any("turned off" in m.subject for m in api.mail.to(email))
    details = sql(
        api,
        "SELECT a.details FROM audit_logs a JOIN users u ON u.id = a.actor_user_id "
        "WHERE u.email = :e AND a.action = 'user.mfa_disabled'",
        e=email,
    )
    assert details[0].details == {"method": "email_code"}
    # 3. Set up the new phone.
    secret, codes = api.enable_mfa(headers)
    assert codes
    challenge = _challenge(api, email)
    assert _finish(api, challenge, code=api.totp(secret)).status_code == 200


def test_step_up_with_an_email_code(api: Api) -> None:
    email, headers, _ = _account(api)
    assert api.client.post("/api/v1/auth/mfa/email-code", headers=headers).status_code == 202
    verified = api.client.post(
        "/api/v1/auth/mfa/verify", json={"email_code": _code(api, email)}, headers=headers
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["mfa_verified_at"]


def test_email_codes_are_refused_right_after_a_password_reset(api: Api) -> None:
    email, _, secret = _account(api)
    api.client.post("/api/v1/auth/password/forgot", json={"email": email})
    reset = api.mail.last_token(email, "/reset-password")
    new_password = "a brand new passphrase"
    assert (
        api.client.post(
            "/api/v1/auth/password/reset", json={"token": reset, "new_password": new_password}
        ).status_code
        == 204
    )
    # The mailbox alone (reset link + email code) must not be enough to get in.
    challenge = str(api.login(email, password=new_password).json()["challenge_token"])
    refused = _send_for_sign_in(api, challenge)
    assert refused.status_code == 403
    assert refused.json()["code"] == "email_code_unavailable"
    # The authenticator still works.
    assert _finish(api, challenge, code=api.totp(secret)).status_code == 200


def test_sending_is_limited_and_needs_2fa(make_api: Any) -> None:
    api: Api = make_api(rate_limit_mfa_email_per_user=2)
    email, _, _ = _account(api)
    challenge = _challenge(api, email)
    assert [_send_for_sign_in(api, challenge).status_code for _ in range(3)] == [202, 202, 429]
    assert _send_for_sign_in(api, "x" * 43).status_code == 401  # needs a real challenge

    plain = api.signup(f"plain-{uuid.uuid4().hex[:8]}@example.com")
    no_mfa = api.client.post("/api/v1/auth/mfa/email-code", headers=plain)
    assert no_mfa.status_code == 403
    assert no_mfa.json()["code"] == "mfa_enrollment_required"
