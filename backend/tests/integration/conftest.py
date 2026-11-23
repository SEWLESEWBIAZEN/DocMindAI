"""Integration fixtures. Requires `docker compose up -d postgres redis minio`
and a .env (see .env.example). Run with: uv run pytest -m integration
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from documind.application.ports import EmailMessage
from documind.container import Container, build_container
from documind.core.config import Environment, Settings
from documind.main import create_app

BACKEND_DIR = Path(__file__).resolve().parents[2]
PASSWORD = "correct horse battery staple"
# Every table holding per-test data (roles/permissions are seed data and stay).
DATA_TABLES = [
    "processing_jobs",
    "document_chunks",
    "document_pages",
    "document_collection_items",
    "document_collections",
    "document_versions",
    "documents",
    "audit_logs",
    "invitations",
    "account_tokens",
    "refresh_tokens",
    "workspace_members",
    "workspaces",
    "organizations",
    "users",
]
TEST_REDIS_DB = 15


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    return Settings()  # reads the environment / .env like the real app


@pytest.fixture(scope="session")
def test_database_url(live_settings: Settings) -> str:
    """URL of the dedicated `<db>_test` database created by the Postgres init script."""
    url = make_url(live_settings.database_url.get_secret_value())
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def migrated_test_db(test_database_url: str) -> str:
    subprocess.run(  # noqa: S603 - fixed argv
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            f"database_url={test_database_url}",
            "upgrade",
            "head",
        ],
        cwd=BACKEND_DIR,
        check=True,
        capture_output=True,
    )
    return test_database_url


TEST_BUCKET = "documind-integration-tests"


@pytest.fixture(scope="session")
def test_bucket(live_settings: Settings) -> Iterator[str]:
    """A private bucket used only by tests; emptied before and after the session."""
    from documind.infrastructure.storage.bootstrap import ensure_private_bucket
    from documind.infrastructure.storage.client import create_s3_client

    client = create_s3_client(live_settings)
    ensure_private_bucket(client, TEST_BUCKET, live_settings.s3_region)
    _empty_bucket(client, TEST_BUCKET)
    yield TEST_BUCKET
    _empty_bucket(client, TEST_BUCKET)
    client.close()


def _empty_bucket(client: Any, bucket: str) -> None:
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            client.delete_object(Bucket=bucket, Key=obj["Key"])


@pytest.fixture
def identity_settings(live_settings: Settings, migrated_test_db: str, test_bucket: str) -> Settings:
    redis_url = make_url(live_settings.redis_url.get_secret_value()).set(
        database=str(TEST_REDIS_DB)
    )
    return live_settings.model_copy(
        update={
            "environment": Environment.TEST,
            "log_level": "WARNING",
            "database_url": SecretStr(migrated_test_db),
            "redis_url": SecretStr(redis_url.render_as_string(hide_password=False)),
            "auth_cookie_secure": False,  # TestClient talks plain http
            "password_hash_time_cost": 1,
            "password_hash_memory_kib": 8,
            "password_hash_parallelism": 1,
            # Generous limits; rate limiting has dedicated tests.
            "rate_limit_login_per_ip": 1000,
            "rate_limit_login_per_email": 1000,
            "rate_limit_auth_per_ip": 1000,
            "rate_limit_refresh_per_ip": 1000,
            "rate_limit_mfa_per_user": 1000,
            "public_app_url": "https://app.test.example",
            "s3_bucket": test_bucket,
            "embedding_provider": "local",
            "llm_provider": "local",
            "rate_limit_ai_per_user": 1000,
            "processing_retry_base_seconds": 1,
        }
    )


async def _reset_state(settings: Settings) -> None:
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    try:
        await redis.flushdb()
    finally:
        await redis.aclose()


@dataclass
class Mailbox:
    """In-memory EmailSender: captures messages so tests can follow links."""

    sent: list[EmailMessage] = field(default_factory=list)

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)

    def to(self, address: str) -> list[EmailMessage]:
        return [m for m in self.sent if m.to == address]

    def last_token(self, address: str, path: str) -> str:
        for message in reversed(self.to(address)):
            match = re.search(rf"{re.escape(path)}\?token=([\w\-]+)", message.text)
            if match:
                return str(match.group(1))
        raise AssertionError(f"No {path} link emailed to {address}")


@dataclass
class RecordingDispatcher:
    """JobDispatcher for API tests: records job IDs instead of publishing to Celery."""

    dispatched: list[uuid.UUID] = field(default_factory=list)
    fail: bool = False

    async def dispatch(self, job_id: uuid.UUID, *, countdown_seconds: int | None = None) -> bool:
        if self.fail:
            return False
        self.dispatched.append(job_id)
        return True


@dataclass
class Api:
    client: TestClient
    mail: Mailbox
    settings: Settings
    dispatcher: RecordingDispatcher = field(default_factory=RecordingDispatcher)
    clock: dict[str, float] = field(default_factory=lambda: {"offset": 0.0})

    # ---- two-factor helpers -----------------------------------------------

    def totp(self, secret: str) -> str:
        """A code for a fresh time step. The server's TOTP clock is shifted by the
        same offset (see make_api), so each call yields a new, unreplayed step."""
        from documind.domain.identity import totp as totp_lib

        # Advance exactly one period first, so client and server agree on the step
        # and every call yields a step later than any previously used one.
        self.clock["offset"] += totp_lib.PERIOD_SECONDS
        now = time.time() + self.clock["offset"]
        return totp_lib.code_at_step(secret, totp_lib.time_step(now))

    def enable_mfa(self, headers: dict[str, str]) -> tuple[str, list[str]]:
        setup = self.client.post(
            "/api/v1/me/mfa/totp/setup", json={"password": PASSWORD}, headers=headers
        )
        assert setup.status_code == 200, setup.text
        secret = str(setup.json()["secret"])
        confirm = self.client.post(
            "/api/v1/me/mfa/totp/confirm", json={"code": self.totp(secret)}, headers=headers
        )
        assert confirm.status_code == 200, confirm.text
        return secret, list(confirm.json()["recovery_codes"])

    def step_up(self, headers: dict[str, str], secret: str) -> dict[str, str]:
        response = self.client.post(
            "/api/v1/auth/mfa/verify", json={"code": self.totp(secret)}, headers=headers
        )
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def elevated(self, headers: dict[str, str]) -> dict[str, str]:
        """Enable 2FA for this user and return step-up (delete-capable) headers."""
        secret, _ = self.enable_mfa(headers)
        return self.step_up(headers, secret)

    # ---- convenience flows -------------------------------------------------

    def register(self, email: str, password: str = PASSWORD, name: str = "Test User") -> Any:
        return self.client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "display_name": name},
        )

    def verify(self, email: str) -> Any:
        token = self.mail.last_token(email, "/verify-email")
        return self.client.post("/api/v1/auth/verify-email", json={"token": token})

    def login(self, email: str, password: str = PASSWORD) -> Any:
        return self.client.post("/api/v1/auth/login", json={"email": email, "password": password})

    def signup(self, email: str, name: str = "Test User") -> dict[str, str]:
        """Register, verify and sign in. Returns Authorization headers."""
        assert self.register(email, name=name).status_code == 202
        assert self.verify(email).status_code == 200
        response = self.login(email)
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def me(self, headers: dict[str, str]) -> Any:
        return self.client.get("/api/v1/me", headers=headers).json()

    def create_org(self, headers: dict[str, str], name: str = "Acme") -> dict[str, Any]:
        response = self.client.post("/api/v1/organizations", json={"name": name}, headers=headers)
        assert response.status_code == 201, response.text
        result: dict[str, Any] = response.json()
        return result

    def add_member(
        self, org_id: str, owner: dict[str, str], email: str, role: str
    ) -> dict[str, str]:
        """Sign up `email`, invite them with `role`, and accept. Returns their headers."""
        headers = self.signup(email)
        invited = self.client.post(
            f"/api/v1/workspaces/{org_id}/invitations",
            json={"email": email, "role": role},
            headers=owner,
        )
        assert invited.status_code == 201, invited.text
        token = self.mail.last_token(email, "/invitations/accept")
        accepted = self.client.post(
            "/api/v1/invitations/accept", json={"token": token}, headers=headers
        )
        assert accepted.status_code == 200, accepted.text
        return headers

    def member_id(self, org_id: str, headers: dict[str, str], email: str) -> str:
        members = self.client.get(f"/api/v1/workspaces/{org_id}/members", headers=headers).json()
        return str(next(m["id"] for m in members if m["email"] == email))

    def use_refresh_cookie(self, value: str | None) -> None:
        self.client.cookies.clear()
        if value is not None:
            self.client.cookies.set(
                self.settings.refresh_cookie_name,
                value,
                domain="testserver.local",
                path="/api/v1/auth",
            )

    def refresh(self, csrf: bool = True) -> Any:
        headers = {"X-CSRF-Protection": "1"} if csrf else {}
        return self.client.post("/api/v1/auth/refresh", headers=headers)


@pytest.fixture
def mailbox() -> Mailbox:
    return Mailbox()


@pytest.fixture
def make_api(
    identity_settings: Settings, mailbox: Mailbox, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Any]:
    """Factory so a test can tweak settings (e.g. rate limits) before the app starts."""
    from documind.application.identity import mfa_service

    clients: list[TestClient] = []
    clock = {"offset": 0.0}
    monkeypatch.setattr(mfa_service, "_totp_clock", lambda: time.time() + clock["offset"])

    def _make(container_overrides: dict[str, Any] | None = None, **overrides: Any) -> Api:
        settings = identity_settings.model_copy(update=overrides)
        asyncio.run(_reset_state(settings))
        dispatcher = RecordingDispatcher()
        container_overrides = {"job_dispatcher": dispatcher, **(container_overrides or {})}

        def factory(s: Settings) -> Container:
            container = build_container(s)
            container.email_sender = mailbox
            for name, value in container_overrides.items():
                setattr(container, name, value)
            return container

        client = TestClient(
            create_app(settings, container_factory=factory), base_url="http://testserver.local"
        )
        client.__enter__()
        clients.append(client)
        api = Api(client=client, mail=mailbox, settings=settings, clock=clock)
        if isinstance(container_overrides["job_dispatcher"], RecordingDispatcher):
            api.dispatcher = container_overrides["job_dispatcher"]
        return api

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def api(make_api: Any) -> Api:
    result: Api = make_api()
    return result
