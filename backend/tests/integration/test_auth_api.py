"""Authentication flows against real PostgreSQL and Redis."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from documind.application.identity import auth_service
from tests.integration.conftest import PASSWORD, Api

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


# --- registration ---


def test_registration_creates_user_and_personal_workspace(api: Api) -> None:
    response = api.register("Ada@Example.com", name="  Ada   Lovelace ")
    assert response.status_code == 202

    rows = _sql(
        api,
        "SELECT u.email, u.display_name, u.email_verified_at, w.kind, w.name, r.key, "
        "u.default_workspace_id = w.id FROM users u "
        "JOIN workspace_members m ON m.user_id = u.id JOIN workspaces w ON w.id = m.workspace_id "
        "JOIN roles r ON r.id = m.role_id",
    )
    assert rows == [
        ("ada@example.com", "Ada Lovelace", None, "personal", "Personal", "owner", True)
    ]
    # Password stored only as an Argon2id hash.
    (stored,) = _sql(api, "SELECT password_hash FROM users")[0]
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored
    assert api.mail.to("ada@example.com")[0].subject == "Verify your DocuMind email"


def test_registration_does_not_reveal_existing_accounts(api: Api) -> None:
    first = api.register("ada@example.com")
    api.verify("ada@example.com")
    second = api.register("ADA@example.com", password="a different password 1")
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert _sql(api, "SELECT count(*) FROM users")[0][0] == 1
    # The real owner is told someone tried to sign up with their address.
    assert api.mail.to("ada@example.com")[-1].subject == "Someone tried to sign up with your email"


def test_registration_validates_input(api: Api) -> None:
    assert api.register("ada@example.com", password="short").status_code == 422
    assert api.register("not-an-email").status_code == 422
    weak = api.register("ada@example.com", password="ada@example.com")
    assert weak.status_code == 422
    assert weak.json()["code"] == "validation_failed"
    extra = api.client.post(
        "/api/v1/auth/register",
        json={
            "email": "a@example.com",
            "password": PASSWORD,
            "display_name": "A",
            "is_admin": True,
        },
    )
    assert extra.status_code == 422  # unknown fields rejected


# --- verification + login ---


def test_login_requires_verified_email(api: Api) -> None:
    api.register("ada@example.com")
    blocked = api.login("ada@example.com")
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "email_not_verified"

    assert api.verify("ada@example.com").status_code == 200
    ok = api.login("ada@example.com")
    assert ok.status_code == 200
    body = ok.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert body["user"]["email_verified"] is True
    assert "password_hash" not in str(body)


def test_login_failures_are_indistinguishable(api: Api) -> None:
    api.signup("ada@example.com")
    wrong = api.login("ada@example.com", password="wrong password 123")
    unknown = api.login("nobody@example.com")
    assert wrong.status_code == unknown.status_code == 401
    assert (
        wrong.json()
        == unknown.json()
        == {"detail": "Invalid email or password.", "code": "invalid_credentials"}
    )
    assert wrong.headers["www-authenticate"] == "Bearer"


def test_verification_token_is_single_use(api: Api) -> None:
    api.register("ada@example.com")
    token = api.mail.last_token("ada@example.com", "/verify-email")
    assert api.client.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 200
    reused = api.client.post("/api/v1/auth/verify-email", json={"token": token})
    assert reused.status_code == 400
    assert reused.json()["code"] == "invalid_token"
    bogus = api.client.post("/api/v1/auth/verify-email", json={"token": "x" * 43})
    assert bogus.status_code == 400


def test_expired_verification_token_rejected(api: Api) -> None:
    api.register("ada@example.com")
    _sql(api, "UPDATE account_tokens SET expires_at = now() - interval '1 minute'")
    assert api.verify("ada@example.com").status_code == 400


def test_resend_verification_is_uniform(api: Api) -> None:
    api.register("ada@example.com")
    known = api.client.post("/api/v1/auth/verify-email/resend", json={"email": "ada@example.com"})
    unknown = api.client.post(
        "/api/v1/auth/verify-email/resend", json={"email": "nobody@example.com"}
    )
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert len(api.mail.to("ada@example.com")) == 2
    assert api.mail.to("nobody@example.com") == []
    # The newest link works, the superseded one doesn't.
    assert api.verify("ada@example.com").status_code == 200


def test_refresh_cookie_attributes(api: Api) -> None:
    api.register("ada@example.com")
    api.verify("ada@example.com")
    cookie = api.login("ada@example.com").headers["set-cookie"]
    assert cookie.startswith(f"{api.settings.refresh_cookie_name}=")
    lowered = cookie.lower()
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert "path=/api/v1/auth" in lowered


# --- access tokens ---


def test_protected_endpoints_require_valid_access_token(api: Api) -> None:
    headers = api.signup("ada@example.com")
    assert api.client.get("/api/v1/me", headers=headers).status_code == 200
    missing = api.client.get("/api/v1/me")
    assert missing.status_code == 401
    assert missing.json()["code"] == "not_authenticated"
    for bad in ("Bearer garbage", "Bearer " + headers["Authorization"][7:-2] + "xx", "Basic abc"):
        assert api.client.get("/api/v1/me", headers={"Authorization": bad}).status_code == 401


def test_profile_update(api: Api) -> None:
    headers = api.signup("ada@example.com")
    response = api.client.patch(
        "/api/v1/me", json={"display_name": " Countess  Ada "}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "Countess Ada"
    assert set(response.json()) == {
        "id",
        "email",
        "display_name",
        "email_verified",
        "mfa_enabled",
        "mfa_method",
        "default_workspace_id",
        "created_at",
    }


# --- refresh rotation ---


def _login_cookie(api: Api, email: str) -> str:
    api.register(email)
    api.verify(email)
    response = api.login(email)
    return str(response.cookies[api.settings.refresh_cookie_name])


def test_refresh_requires_csrf_header(api: Api) -> None:
    api.use_refresh_cookie(_login_cookie(api, "ada@example.com"))
    response = api.refresh(csrf=False)
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_header_missing"


def test_refresh_rotates_token(api: Api) -> None:
    first = _login_cookie(api, "ada@example.com")
    api.use_refresh_cookie(first)
    response = api.refresh()
    assert response.status_code == 200
    second = response.cookies[api.settings.refresh_cookie_name]
    assert second != first
    assert (
        api.client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {response.json()['access_token']}"}
        ).status_code
        == 200
    )
    # Only hashes are stored.
    stored = {row[0] for row in _sql(api, "SELECT token_hash FROM refresh_tokens")}
    assert first not in stored
    assert second not in stored


def test_reused_refresh_token_revokes_the_session(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_service, "ROTATION_GRACE", timedelta(0))
    stolen = _login_cookie(api, "ada@example.com")
    api.use_refresh_cookie(stolen)
    legit = api.refresh().cookies[api.settings.refresh_cookie_name]

    api.use_refresh_cookie(stolen)  # attacker replays the old token
    assert api.refresh().status_code == 401
    api.use_refresh_cookie(legit)  # the whole family is now dead
    assert api.refresh().status_code == 401
    assert (
        _sql(
            api,
            "SELECT count(*) FROM audit_logs WHERE action = 'auth.refresh_token_reuse_detected'",
        )[0][0]
        == 1
    )


def test_concurrent_refresh_within_grace_does_not_revoke(api: Api) -> None:
    first = _login_cookie(api, "ada@example.com")
    api.use_refresh_cookie(first)
    second = api.refresh().cookies[api.settings.refresh_cookie_name]
    api.use_refresh_cookie(first)  # a second tab raced with the same token
    assert api.refresh().status_code == 401
    api.use_refresh_cookie(second)
    assert api.refresh().status_code == 200


def test_expired_refresh_token_rejected(api: Api) -> None:
    api.use_refresh_cookie(_login_cookie(api, "ada@example.com"))
    _sql(api, "UPDATE refresh_tokens SET expires_at = now() - interval '1 second'")
    response = api.refresh()
    assert response.status_code == 401
    assert "max-age=0" in response.headers.get("set-cookie", "").lower()  # cookie cleared


def test_refresh_without_cookie(api: Api) -> None:
    api.use_refresh_cookie(None)
    assert api.refresh().status_code == 401


# --- logout ---


def test_logout_revokes_session(api: Api) -> None:
    cookie = _login_cookie(api, "ada@example.com")
    api.use_refresh_cookie(cookie)
    response = api.client.post("/api/v1/auth/logout", headers={"X-CSRF-Protection": "1"})
    assert response.status_code == 204
    assert "max-age=0" in response.headers["set-cookie"].lower()
    api.use_refresh_cookie(cookie)
    assert api.refresh().status_code == 401
    # Idempotent, and doesn't reveal anything about unknown tokens.
    api.use_refresh_cookie("x" * 43)
    assert (
        api.client.post("/api/v1/auth/logout", headers={"X-CSRF-Protection": "1"}).status_code
        == 204
    )


def test_logout_everywhere_invalidates_access_tokens_immediately(api: Api) -> None:
    headers = api.signup("ada@example.com")
    other_device = api.login("ada@example.com").json()["access_token"]
    assert api.client.post("/api/v1/auth/logout-all", headers=headers).status_code == 204
    for token in (headers["Authorization"][7:], other_device):
        assert (
            api.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code
            == 401
        )
    assert _sql(api, "SELECT count(*) FROM refresh_tokens WHERE revoked_at IS NULL")[0][0] == 0


# --- password reset / change ---


def test_forgot_password_is_uniform(api: Api) -> None:
    api.signup("ada@example.com")
    known = api.client.post("/api/v1/auth/password/forgot", json={"email": "ada@example.com"})
    unknown = api.client.post("/api/v1/auth/password/forgot", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert api.mail.to("nobody@example.com") == []


def test_password_reset_flow(api: Api) -> None:
    old_headers = api.signup("ada@example.com")
    api.client.post("/api/v1/auth/password/forgot", json={"email": "ada@example.com"})
    token = api.mail.last_token("ada@example.com", "/reset-password")

    weak = api.client.post(
        "/api/v1/auth/password/reset", json={"token": token, "new_password": "short"}
    )
    assert weak.status_code == 422
    new_password = "a brand new passphrase"
    ok = api.client.post(
        "/api/v1/auth/password/reset", json={"token": token, "new_password": new_password}
    )
    assert ok.status_code == 204

    assert api.client.get("/api/v1/me", headers=old_headers).status_code == 401  # old sessions gone
    assert api.login("ada@example.com").status_code == 401
    assert api.login("ada@example.com", password=new_password).status_code == 200
    reused = api.client.post(
        "/api/v1/auth/password/reset", json={"token": token, "new_password": "yet another phrase"}
    )
    assert reused.status_code == 400
    assert api.mail.to("ada@example.com")[-1].subject == "Your DocuMind password was changed"


def test_only_latest_reset_link_works(api: Api) -> None:
    api.signup("ada@example.com")
    api.client.post("/api/v1/auth/password/forgot", json={"email": "ada@example.com"})
    first = api.mail.last_token("ada@example.com", "/reset-password")
    api.client.post("/api/v1/auth/password/forgot", json={"email": "ada@example.com"})
    response = api.client.post(
        "/api/v1/auth/password/reset",
        json={"token": first, "new_password": "a brand new passphrase"},
    )
    assert response.status_code == 400


def test_change_password(api: Api) -> None:
    headers = api.signup("ada@example.com")
    other = api.login("ada@example.com").json()["access_token"]
    wrong = api.client.post(
        "/api/v1/me/password",
        json={"current_password": "nope nope nope", "new_password": "fresh passphrase 1"},
        headers=headers,
    )
    assert wrong.status_code == 401
    ok = api.client.post(
        "/api/v1/me/password",
        json={"current_password": PASSWORD, "new_password": "fresh passphrase 1"},
        headers=headers,
    )
    assert ok.status_code == 200
    new_headers = {"Authorization": f"Bearer {ok.json()['access_token']}"}
    assert api.client.get("/api/v1/me", headers=new_headers).status_code == 200
    assert (
        api.client.get("/api/v1/me", headers={"Authorization": f"Bearer {other}"}).status_code
        == 401
    )


# --- rate limiting ---


def test_login_is_rate_limited_per_email(make_api: Any) -> None:
    api: Api = make_api(rate_limit_login_per_email=3)
    api.signup("ada@example.com")  # 1 login
    assert api.login("ada@example.com", password="wrong password 1").status_code == 401
    assert api.login("ada@example.com", password="wrong password 2").status_code == 401
    limited = api.login("ada@example.com")  # correct password, but over the limit
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) > 0
    # A different account from the same client is unaffected by the per-email limit.
    assert api.register("bob@example.com").status_code == 202


def test_auth_endpoints_rate_limited_per_ip(make_api: Any) -> None:
    api: Api = make_api(rate_limit_auth_per_ip=2)
    for i in range(2):
        assert (
            api.client.post(
                "/api/v1/auth/password/forgot", json={"email": f"u{i}@example.com"}
            ).status_code
            == 202
        )
    assert (
        api.client.post(
            "/api/v1/auth/password/forgot", json={"email": "u9@example.com"}
        ).status_code
        == 429
    )


def test_seeded_roles_match_code_catalog(api: Api) -> None:
    from documind.domain.identity.permissions import ROLE_PERMISSIONS, ROLE_RANK

    rows = _sql(
        api,
        "SELECT r.key, r.rank, p.key FROM roles r JOIN role_permissions rp ON rp.role_id = r.id "
        "JOIN permissions p ON p.id = rp.permission_id",
    )
    db: dict[str, set[str]] = {}
    ranks: dict[str, int] = {}
    for role, rank, permission in rows:
        db.setdefault(role, set()).add(permission)
        ranks[role] = rank
    assert db == {role.value: {p.value for p in perms} for role, perms in ROLE_PERMISSIONS.items()}
    assert ranks == {role.value: rank for role, rank in ROLE_RANK.items()}
