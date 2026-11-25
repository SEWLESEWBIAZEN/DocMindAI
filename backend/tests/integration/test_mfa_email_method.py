"""Email-only 2FA, and organizations that allow or refuse email codes (ADR 0028)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.integration.conftest import PASSWORD, Api
from tests.integration.test_mfa_email_codes import _code
from tests.integration.test_processing_pipeline import sql
from tests.integration.test_usage_and_quotas import assign

pytestmark = pytest.mark.integration


def _email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def enable_email_mfa(api: Api, headers: dict[str, str], email: str) -> list[str]:
    started = api.client.post(
        "/api/v1/me/mfa/email/setup", json={"password": PASSWORD}, headers=headers
    )
    assert started.status_code == 202, started.text
    confirmed = api.client.post(
        "/api/v1/me/mfa/email/confirm", json={"email_code": _code(api, email)}, headers=headers
    )
    assert confirmed.status_code == 200, confirmed.text
    codes: list[str] = confirmed.json()["recovery_codes"]
    return codes


def email_step_up(api: Api, headers: dict[str, str], email: str) -> dict[str, str]:
    assert api.client.post("/api/v1/auth/mfa/email-code", headers=headers).status_code == 202
    verified = api.client.post(
        "/api/v1/auth/mfa/verify", json={"email_code": _code(api, email)}, headers=headers
    )
    assert verified.status_code == 200, verified.text
    return {"Authorization": f"Bearer {verified.json()['access_token']}"}


def test_email_only_2fa_without_an_authenticator_app(api: Api) -> None:
    email = _email("mail2fa")
    headers = api.signup(email)
    wrong = api.client.post(
        "/api/v1/me/mfa/email/setup", json={"password": PASSWORD + "x"}, headers=headers
    )
    assert wrong.status_code == 401
    codes = enable_email_mfa(api, headers, email)
    assert len(codes) == 10  # recovery codes, for losing the mailbox
    status = api.client.get("/api/v1/me/mfa", headers=headers).json()
    assert status["enabled"] is True
    assert status["method"] == "email"
    assert api.me(headers)["mfa_method"] == "email"

    # Sign-in offers email (no authenticator) and recovery codes.
    first = api.login(email).json()
    assert first["methods"] == ["email", "recovery_code"]
    challenge = first["challenge_token"]
    assert (
        api.client.post(
            "/api/v1/auth/login/mfa/email-code", json={"challenge_token": challenge}
        ).status_code
        == 202
    )
    done = api.client.post(
        "/api/v1/auth/login/mfa",
        json={"challenge_token": challenge, "email_code": _code(api, email)},
    )
    assert done.status_code == 200, done.text
    # An authenticator code can't be used: there is none.
    assert (
        api.client.post(
            "/api/v1/auth/login/mfa",
            json={"challenge_token": api.login(email).json()["challenge_token"], "code": "123456"},
        ).status_code
        == 401
    )

    # Switching to an authenticator later makes it the primary factor.
    setup = api.client.post(
        "/api/v1/me/mfa/totp/setup", json={"password": PASSWORD}, headers=headers
    )
    assert setup.status_code == 200, setup.text
    confirm = api.client.post(
        "/api/v1/me/mfa/totp/confirm",
        json={"code": api.totp(setup.json()["secret"])},
        headers=headers,
    )
    assert confirm.status_code == 200
    assert api.client.get("/api/v1/me/mfa", headers=headers).json()["method"] == "authenticator"
    assert api.login(email).json()["methods"] == ["authenticator", "email", "recovery_code"]


def test_new_recovery_codes_with_an_email_code(api: Api) -> None:
    email = _email("regen")
    headers = api.signup(email)
    api.enable_mfa(headers)  # authenticator user who doesn't have the app at hand
    assert api.client.post("/api/v1/auth/mfa/email-code", headers=headers).status_code == 202
    fresh = api.client.post(
        "/api/v1/me/mfa/recovery-codes", json={"email_code": _code(api, email)}, headers=headers
    )
    assert fresh.status_code == 200, fresh.text
    assert len(fresh.json()["recovery_codes"]) == 10
    # Recovery codes can't be used to mint new ones, and some factor is always needed.
    assert (
        api.client.post(
            "/api/v1/me/mfa/recovery-codes",
            json={"recovery_code": fresh.json()["recovery_codes"][0]},
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        api.client.post("/api/v1/me/mfa/recovery-codes", json={}, headers=headers).status_code
        == 422
    )


def test_organizations_can_refuse_email_codes(api: Api) -> None:
    owner_email = _email("owner")
    owner = api.signup(owner_email)
    org = str(api.create_org(owner, "Strict Org")["id"])
    member_email = _email("member")
    member = api.add_member(org, owner, member_email, "member")
    enable_email_mfa(api, member, member_email)
    assign(api, org, "pro")  # 2FA required

    # Email codes allowed (the default): email 2FA counts.
    summary = next(
        w for w in api.client.get("/api/v1/me/workspaces", headers=member).json() if w["id"] == org
    )
    assert summary["allow_email_mfa"] is True
    assert summary["mfa_enrollment_deadline"] is None

    # Changing the setting needs a fresh check, and one by email code doesn't count once refused.
    secret, _ = api.enable_mfa(owner)
    url = f"/api/v1/workspaces/{org}/mfa-requirement"
    refused = api.client.put(
        url, json={"allow_email_codes": False}, headers=api.step_up(owner, secret)
    )
    assert refused.status_code == 200, refused.text
    assert refused.json()["allow_email_mfa"] is False
    by_email = api.client.put(
        url, json={"allow_email_codes": True}, headers=email_step_up(api, owner, owner_email)
    )
    assert by_email.status_code == 403
    assert by_email.json()["code"] == "mfa_step_up_required"

    # The email-only member now has to add an authenticator, after the grace period.
    summary = next(
        w for w in api.client.get("/api/v1/me/workspaces", headers=member).json() if w["id"] == org
    )
    assert summary["mfa_enrollment_deadline"] is not None
    members = {
        m["email"]: m
        for m in api.client.get(f"/api/v1/workspaces/{org}/members", headers=owner).json()
    }
    assert members[member_email]["mfa_enabled"] is False
    assert members[member_email]["mfa_method"] == "email"
    sql(
        api,
        "UPDATE workspaces SET mfa_enforced_since = now() - interval '15 days' WHERE id = :w",
        w=uuid.UUID(org),
    )
    blocked = api.client.get(f"/api/v1/workspaces/{org}/documents", headers=member)
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "workspace_requires_mfa"
    # Their personal workspace is unaffected.
    personal = str(api.me(member)["default_workspace_id"])
    assert (
        api.client.get(f"/api/v1/workspaces/{personal}/documents", headers=member).status_code
        == 200
    )

    # Allowing email codes again restores access.
    allowed = api.client.put(
        url, json={"allow_email_codes": True}, headers=api.step_up(owner, secret)
    )
    assert allowed.status_code == 200
    assert api.client.get(f"/api/v1/workspaces/{org}/documents", headers=member).status_code == 200
    details = [
        r.details
        for r in sql(
            api,
            "SELECT details FROM audit_logs WHERE workspace_id = :w "
            "AND action = 'workspace.mfa_requirement_changed' ORDER BY created_at",
            w=uuid.UUID(org),
        )
    ]
    assert details == [{"allow_email_codes": False}, {"allow_email_codes": True}]


def test_disable_turns_email_2fa_off(make_api: Any) -> None:
    api: Api = make_api()
    email = _email("off")
    headers = api.signup(email)
    enable_email_mfa(api, headers, email)
    assert api.client.post("/api/v1/auth/mfa/email-code", headers=headers).status_code == 202
    off = api.client.post(
        "/api/v1/me/mfa/disable",
        json={"password": PASSWORD, "email_code": _code(api, email)},
        headers=headers,
    )
    assert off.status_code == 204
    assert api.client.get("/api/v1/me/mfa", headers=headers).json()["method"] is None
    assert "access_token" in api.login(email).json()
