"""Archiving (members) and 2FA-protected deletion (admins), against the real stack."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from documind.application.documents import document_service
from documind.application.identity.common import utcnow
from tests.integration.conftest import PASSWORD, Api
from tests.integration.test_documents_api import pdf, upload

pytestmark = pytest.mark.integration


def _sql(api: Api, sql: str, **params: Any) -> list[Any]:
    async def run() -> list[Any]:
        engine = create_async_engine(api.settings.database_url.get_secret_value())
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql), params)
                return list(result.all()) if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(run())


def org_with_team(api: Api) -> tuple[str, dict[str, dict[str, str]]]:
    owner = api.signup("owner@example.com")
    org = str(api.create_org(owner)["id"])
    team = {
        "owner": owner,
        "admin": api.add_member(org, owner, "admin@example.com", "admin"),
        "mia": api.add_member(org, owner, "mia@example.com", "member"),
        "max": api.add_member(org, owner, "max@example.com", "member"),
        "viewer": api.add_member(org, owner, "vic@example.com", "viewer"),
    }
    return org, team


def docs_url(org: str, doc_id: str = "", action: str = "") -> str:
    url = f"/api/v1/workspaces/{org}/documents"
    if doc_id:
        url += f"/{doc_id}"
    if action:
        url += f"/{action}"
    return url


# --- archiving ---


def test_members_archive_and_restore_only_their_own_uploads(api: Api) -> None:
    org, team = org_with_team(api)
    doc = upload(api, org, team["mia"], pdf("mia"), "mia.pdf").json()

    assert (
        api.client.post(docs_url(org, doc["id"], "archive"), headers=team["max"]).status_code == 403
    )
    assert (
        api.client.post(docs_url(org, doc["id"], "archive"), headers=team["viewer"]).status_code
        == 403
    )

    archived = api.client.post(docs_url(org, doc["id"], "archive"), headers=team["mia"])
    assert archived.status_code == 200
    body = archived.json()
    assert body["archived_at"] is not None
    assert body["archived_by_user_id"] == api.me(team["mia"])["id"]
    # Idempotent.
    assert (
        api.client.post(docs_url(org, doc["id"], "archive"), headers=team["mia"]).status_code == 200
    )

    def titles(**params: str) -> list[str]:
        items = api.client.get(docs_url(org), params=params, headers=team["mia"]).json()["items"]
        return [d["title"] for d in items]

    assert titles() == []
    assert titles(archived="only") == ["mia"]
    assert titles(archived="include") == ["mia"]

    assert (
        api.client.post(docs_url(org, doc["id"], "restore"), headers=team["max"]).status_code == 403
    )
    restored = api.client.post(docs_url(org, doc["id"], "restore"), headers=team["mia"])
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None
    assert titles() == ["mia"]


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_admins_and_owners_archive_any_document(api: Api, role: str) -> None:
    org, team = org_with_team(api)
    doc = upload(api, org, team["mia"], pdf(role), "mia.pdf").json()
    assert (
        api.client.post(docs_url(org, doc["id"], "archive"), headers=team[role]).status_code == 200
    )
    assert (
        api.client.post(docs_url(org, doc["id"], "restore"), headers=team[role]).status_code == 200
    )


def test_archived_documents_are_read_only_but_downloadable(api: Api) -> None:
    org, team = org_with_team(api)
    mia = team["mia"]
    doc = upload(api, org, mia, pdf("ro"), "policy.pdf").json()
    collection = api.client.post(
        f"/api/v1/workspaces/{org}/collections", json={"name": "C"}, headers=mia
    ).json()
    api.client.post(docs_url(org, doc["id"], "archive"), headers=mia)

    rename = api.client.patch(docs_url(org, doc["id"]), json={"title": "x"}, headers=mia)
    assert rename.status_code == 409
    assert rename.json()["code"] == "document_archived"
    version = api.client.post(
        docs_url(org, doc["id"], "versions"), files={"file": ("v2.pdf", pdf("v2"))}, headers=mia
    )
    assert version.status_code == 409
    add = api.client.put(
        f"/api/v1/workspaces/{org}/collections/{collection['id']}/documents/{doc['id']}",
        headers=mia,
    )
    assert add.status_code == 409
    assert (
        api.client.get(docs_url(org, doc["id"], "download-url"), headers=team["viewer"]).status_code
        == 200
    )
    duplicate = upload(api, org, mia, pdf("ro"), "again.pdf")
    assert duplicate.status_code == 409
    assert "archived" in duplicate.json()["detail"]


def test_members_can_still_manage_collections(api: Api) -> None:
    org, team = org_with_team(api)
    collection = api.client.post(
        f"/api/v1/workspaces/{org}/collections", json={"name": "C"}, headers=team["mia"]
    ).json()
    url = f"/api/v1/workspaces/{org}/collections/{collection['id']}"
    assert api.client.delete(url, headers=team["viewer"]).status_code == 403
    assert api.client.delete(url, headers=team["mia"]).status_code == 204


# --- deletion requires admin + 2FA ---


def test_members_cannot_delete_even_with_2fa(api: Api) -> None:
    org, team = org_with_team(api)
    doc = upload(api, org, team["mia"], pdf("m"), "mine.pdf").json()
    response = api.client.delete(docs_url(org, doc["id"]), headers=api.elevated(team["mia"]))
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_admin_delete_requires_2fa_step_up(api: Api, role: str) -> None:
    org, team = org_with_team(api)
    doc = upload(api, org, team["mia"], pdf(f"del-{role}"), "report.pdf").json()
    url = docs_url(org, doc["id"])

    not_enrolled = api.client.delete(url, headers=team[role])
    assert not_enrolled.status_code == 403
    assert not_enrolled.json()["code"] == "mfa_enrollment_required"

    secret, _ = api.enable_mfa(team[role])
    no_step_up = api.client.delete(url, headers=team[role])
    assert no_step_up.status_code == 403
    assert no_step_up.json()["code"] == "mfa_step_up_required"

    elevated = api.step_up(team[role], secret)
    assert api.client.delete(url, headers=elevated).status_code == 204
    assert api.client.get(url, headers=team[role]).status_code == 404
    deleted = _sql(
        api, "SELECT details->>'mfa_verified' FROM audit_logs WHERE action = 'document.deleted'"
    )
    assert deleted == [("true",)]


def test_personal_workspace_owner_also_needs_2fa_to_delete(api: Api) -> None:
    ada = api.signup("ada@example.com")
    ws = next(w["id"] for w in api.client.get("/api/v1/me/workspaces", headers=ada).json())
    doc = upload(api, ws, ada, pdf("personal"), "mine.pdf").json()
    assert api.client.delete(docs_url(ws, doc["id"]), headers=ada).status_code == 403
    assert (
        api.client.post(docs_url(ws, doc["id"], "archive"), headers=ada).status_code == 200
    )  # no 2FA needed
    assert api.client.delete(docs_url(ws, doc["id"]), headers=api.elevated(ada)).status_code == 204


def test_step_up_expires(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    org, team = org_with_team(api)
    doc = upload(api, org, team["mia"], pdf("exp"), "a.pdf").json()
    elevated = api.elevated(team["admin"])
    later = utcnow() + timedelta(seconds=api.settings.mfa_step_up_ttl_seconds + 5)
    monkeypatch.setattr(document_service, "utcnow", lambda: later)
    response = api.client.delete(docs_url(org, doc["id"]), headers=elevated)
    assert response.status_code == 403
    assert response.json()["code"] == "mfa_step_up_required"


def test_refreshed_tokens_are_never_elevated(api: Api) -> None:
    org, team = org_with_team(api)
    doc = upload(api, org, team["mia"], pdf("refresh"), "a.pdf").json()
    api.client.cookies.clear()
    login = api.login("admin@example.com")  # puts admin's refresh cookie in the jar
    admin = {"Authorization": f"Bearer {login.json()['access_token']}"}
    secret, _ = api.enable_mfa(admin)
    api.step_up(admin, secret)
    refreshed = api.refresh()
    assert refreshed.status_code == 200
    token = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    assert (
        api.client.delete(docs_url(org, doc["id"]), headers=token).json()["code"]
        == "mfa_step_up_required"
    )


# --- 2FA enrollment and verification ---


def test_mfa_enrollment(api: Api) -> None:
    ada = api.signup("ada@example.com")
    wrong_pw = api.client.post(
        "/api/v1/me/mfa/totp/setup", json={"password": "wrong password!"}, headers=ada
    )
    assert wrong_pw.status_code == 401

    setup = api.client.post("/api/v1/me/mfa/totp/setup", json={"password": PASSWORD}, headers=ada)
    assert setup.status_code == 200
    assert setup.headers["cache-control"] == "no-store"
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith(
        "otpauth://totp/DocuMind%3Aada%40example.com?secret="
    )
    assert api.me(ada)["mfa_enabled"] is False

    bad = api.client.post("/api/v1/me/mfa/totp/confirm", json={"code": "000000"}, headers=ada)
    assert bad.status_code == 401
    assert bad.json()["code"] == "invalid_mfa_code"
    confirm = api.client.post(
        "/api/v1/me/mfa/totp/confirm", json={"code": api.totp(secret)}, headers=ada
    )
    assert confirm.status_code == 200
    assert len(confirm.json()["recovery_codes"]) == 10
    assert api.me(ada)["mfa_enabled"] is True
    status = api.client.get("/api/v1/me/mfa", headers=ada).json()
    assert status["enabled"] is True
    assert status["recovery_codes_remaining"] == 10
    again = api.client.post("/api/v1/me/mfa/totp/setup", json={"password": PASSWORD}, headers=ada)
    assert again.json()["code"] == "mfa_already_enabled"
    assert api.mail.to("ada@example.com")[-1].subject == "Two-factor authentication is on"

    # The secret is encrypted at rest; recovery codes are stored only as hashes.
    (stored,) = _sql(api, "SELECT totp_secret_encrypted FROM users")[0]
    assert stored.startswith("v1:")
    assert secret not in stored
    hashes = {row[0] for row in _sql(api, "SELECT code_hash FROM mfa_recovery_codes")}
    assert not hashes & set(confirm.json()["recovery_codes"])


def test_expired_setup_must_restart(api: Api) -> None:
    ada = api.signup("ada@example.com")
    secret = api.client.post(
        "/api/v1/me/mfa/totp/setup", json={"password": PASSWORD}, headers=ada
    ).json()["secret"]
    _sql(api, "UPDATE users SET totp_pending_expires_at = now() - interval '1 second'")
    response = api.client.post(
        "/api/v1/me/mfa/totp/confirm", json={"code": api.totp(secret)}, headers=ada
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_token"


def test_totp_codes_cannot_be_replayed(api: Api) -> None:
    ada = api.signup("ada@example.com")
    secret, _ = api.enable_mfa(ada)
    code = api.totp(secret)
    assert (
        api.client.post("/api/v1/auth/mfa/verify", json={"code": code}, headers=ada).status_code
        == 200
    )
    replay = api.client.post("/api/v1/auth/mfa/verify", json={"code": code}, headers=ada)
    assert replay.status_code == 401
    assert replay.json()["code"] == "invalid_mfa_code"


def test_recovery_codes_are_single_use_and_regenerable(api: Api) -> None:
    ada = api.signup("ada@example.com")
    secret, codes = api.enable_mfa(ada)
    ok = api.client.post(
        "/api/v1/auth/mfa/verify", json={"recovery_code": codes[0].upper()}, headers=ada
    )
    assert ok.status_code == 200
    assert (
        api.client.post(
            "/api/v1/auth/mfa/verify", json={"recovery_code": codes[0]}, headers=ada
        ).status_code
        == 401
    )
    assert api.client.get("/api/v1/me/mfa", headers=ada).json()["recovery_codes_remaining"] == 9
    assert api.mail.to("ada@example.com")[-1].subject == "A recovery code was used on your account"

    fresh = api.client.post(
        "/api/v1/me/mfa/recovery-codes", json={"code": api.totp(secret)}, headers=ada
    )
    assert fresh.status_code == 200
    assert set(fresh.json()["recovery_codes"]).isdisjoint(codes)
    assert (
        api.client.post(
            "/api/v1/auth/mfa/verify", json={"recovery_code": codes[1]}, headers=ada
        ).status_code
        == 401
    )
    new_code = fresh.json()["recovery_codes"][0]
    assert (
        api.client.post(
            "/api/v1/auth/mfa/verify", json={"recovery_code": new_code}, headers=ada
        ).status_code
        == 200
    )


def test_step_up_needs_exactly_one_factor_and_enrollment(api: Api) -> None:
    ada = api.signup("ada@example.com")
    not_enrolled = api.client.post("/api/v1/auth/mfa/verify", json={"code": "123456"}, headers=ada)
    assert not_enrolled.json()["code"] == "mfa_enrollment_required"
    secret, codes = api.enable_mfa(ada)
    both = api.client.post(
        "/api/v1/auth/mfa/verify",
        json={"code": api.totp(secret), "recovery_code": codes[0]},
        headers=ada,
    )
    assert both.status_code == 422
    assert api.client.post("/api/v1/auth/mfa/verify", json={}, headers=ada).status_code == 422


def test_disable_mfa(api: Api) -> None:
    org, team = org_with_team(api)
    admin = team["admin"]
    doc = upload(api, org, team["mia"], pdf("dis"), "a.pdf").json()
    secret, _ = api.enable_mfa(admin)

    wrong = api.client.post(
        "/api/v1/me/mfa/disable", json={"password": PASSWORD, "code": "000000"}, headers=admin
    )
    assert wrong.status_code == 401
    no_pw = api.client.post(
        "/api/v1/me/mfa/disable",
        json={"password": "nope nope nope", "code": api.totp(secret)},
        headers=admin,
    )
    assert no_pw.status_code == 401
    ok = api.client.post(
        "/api/v1/me/mfa/disable",
        json={"password": PASSWORD, "code": api.totp(secret)},
        headers=admin,
    )
    assert ok.status_code == 204
    assert api.me(admin)["mfa_enabled"] is False
    assert (
        api.mail.to("admin@example.com")[-1].subject == "Two-factor authentication was turned off"
    )
    assert (
        api.client.delete(docs_url(org, doc["id"]), headers=admin).json()["code"]
        == "mfa_enrollment_required"
    )


def test_mfa_attempts_are_rate_limited(make_api: Any) -> None:
    api: Api = make_api(rate_limit_mfa_per_user=3)
    ada = api.signup("ada@example.com")
    secret, _ = api.enable_mfa(ada)  # setup + confirm = 2 attempts
    assert (
        api.client.post("/api/v1/auth/mfa/verify", json={"code": "000000"}, headers=ada).status_code
        == 401
    )
    limited = api.client.post(
        "/api/v1/auth/mfa/verify", json={"code": api.totp(secret)}, headers=ada
    )
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"


def test_mfa_events_are_audited(api: Api) -> None:
    ada = api.signup("ada@example.com")
    secret, _ = api.enable_mfa(ada)
    api.client.post("/api/v1/auth/mfa/verify", json={"code": "000000"}, headers=ada)
    api.step_up(ada, secret)
    actions = [
        row[0]
        for row in _sql(api, "SELECT action FROM audit_logs WHERE action LIKE '%mfa%' ORDER BY id")
    ]
    assert actions == [
        "user.mfa_setup_started",
        "user.mfa_enabled",
        "auth.mfa_failed",
        "auth.mfa_verified",
    ]
