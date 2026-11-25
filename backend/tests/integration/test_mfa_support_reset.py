"""Support-assisted 2FA reset for someone who lost every factor (ADR 0030)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from documind.application.identity.common import RequestMeta
from documind.application.identity.mfa_reset import MfaResetService
from documind.container import build_container
from tests.integration.conftest import Api
from tests.integration.test_processing_pipeline import sql

pytestmark = pytest.mark.integration

REASON = "Lost my phone and my email account was closed when I changed jobs."


def _account(api: Api) -> tuple[str, dict[str, str], str]:
    email = f"lost-{uuid.uuid4().hex[:8]}@example.com"
    headers = api.signup(email)
    secret, _ = api.enable_mfa(headers)
    return email, headers, secret


def request_reset(api: Api, email: str, reason: str = REASON) -> Any:
    challenge = api.login(email).json()["challenge_token"]
    return api.client.post(
        "/api/v1/auth/login/mfa/reset-request",
        json={"challenge_token": challenge, "reason": reason},
    )


def support(api: Api, action: str, request_id: str) -> Any:
    """What `documind-manage mfa-reset approve|deny` does."""

    async def go() -> Any:
        c = build_container(api.settings)
        try:
            async with c.session_factory() as session:
                service = MfaResetService(session, api.mail, RequestMeta(), delay_hours=72)
                if action == "approve":
                    return await service.approve(
                        uuid.UUID(request_id), "agent@support", "video call + ID"
                    )
                return await service.deny(uuid.UUID(request_id), "agent@support", "no match")
        finally:
            await c.aclose()

    return asyncio.run(go())


def _request_id(api: Api, email: str) -> str:
    rows = sql(
        api,
        "SELECT r.id, r.status FROM mfa_reset_requests r JOIN users u ON u.id = r.user_id "
        "WHERE u.email = :e ORDER BY r.created_at DESC",
        e=email,
    )
    return str(rows[0].id)


def _status(api: Api, request_id: str) -> str:
    return str(
        sql(api, "SELECT status FROM mfa_reset_requests WHERE id = :i", i=uuid.UUID(request_id))[
            0
        ].status
    )


def test_approved_reset_takes_effect_after_the_waiting_period(api: Api) -> None:
    email, _, _ = _account(api)
    sent = request_reset(api, email)
    assert sent.status_code == 202, sent.text
    assert any("asked to reset two-factor" in m.subject for m in api.mail.to(email))
    assert request_reset(api, email).json()["code"] == "mfa_reset_open"  # one at a time
    rid = _request_id(api, email)

    support(api, "approve", rid)
    assert _status(api, rid) == "approved"
    assert any(m.subject == "Two-factor reset approved" for m in api.mail.to(email))
    # Still protected during the waiting period.
    assert api.login(email).json()["mfa_required"] is True

    sql(
        api,
        "UPDATE mfa_reset_requests SET effective_at = now() - interval '1 minute' WHERE id = :i",
        i=uuid.UUID(rid),
    )
    signed_in = api.login(email).json()
    assert "access_token" in signed_in  # password only: 2FA was reset
    assert signed_in["user"]["mfa_enabled"] is False
    assert _status(api, rid) == "completed"
    assert (
        sql(
            api,
            "SELECT count(*) AS n FROM mfa_recovery_codes c JOIN users u ON u.id = c.user_id "
            "WHERE u.email = :e",
            e=email,
        )[0].n
        == 0
    )
    assert any(m.subject == "Two-factor authentication was reset" for m in api.mail.to(email))


def test_signing_in_with_a_real_factor_cancels_the_reset(api: Api) -> None:
    """The owner still has their phone: someone else asked for this reset."""
    email, _, secret = _account(api)
    request_reset(api, email)
    rid = _request_id(api, email)
    support(api, "approve", rid)
    challenge = api.login(email).json()["challenge_token"]
    done = api.client.post(
        "/api/v1/auth/login/mfa", json={"challenge_token": challenge, "code": api.totp(secret)}
    )
    assert done.status_code == 200
    assert _status(api, rid) == "cancelled"
    assert any(m.subject == "Two-factor reset cancelled" for m in api.mail.to(email))
    sql(
        api,
        "UPDATE mfa_reset_requests SET effective_at = now() - interval '1 minute' WHERE id = :i",
        i=uuid.UUID(rid),
    )
    assert api.login(email).json()["mfa_required"] is True  # nothing was reset


def test_denied_requests_change_nothing(api: Api) -> None:
    email, _, _ = _account(api)
    request_reset(api, email)
    rid = _request_id(api, email)
    support(api, "deny", rid)
    assert _status(api, rid) == "denied"
    assert any(m.subject == "Two-factor reset not approved" for m in api.mail.to(email))
    assert api.login(email).json()["mfa_required"] is True
    with pytest.raises(Exception, match="denied"):
        support(api, "approve", rid)  # decisions are final


def test_requests_need_the_password_step_and_a_reason(api: Api) -> None:
    email, _, _ = _account(api)
    assert request_reset(api, email, reason="lost").status_code == 422
    forged = api.client.post(
        "/api/v1/auth/login/mfa/reset-request",
        json={"challenge_token": "x" * 43, "reason": REASON},
    )
    assert forged.status_code == 401
    actions = {
        r.action
        for r in sql(
            api,
            "SELECT a.action FROM audit_logs a JOIN users u ON u.id = a.actor_user_id "
            "WHERE u.email = :e",
            e=email,
        )
    }
    assert "user.mfa_reset_requested" not in actions
