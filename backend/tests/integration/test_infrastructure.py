"""End-to-end checks against real PostgreSQL, Redis and S3-compatible storage."""

import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from documind.cli import main as cli_main
from documind.core.config import Settings
from documind.infrastructure.cache.redis import create_redis_client
from documind.infrastructure.storage.bootstrap import ensure_private_bucket
from documind.infrastructure.storage.client import create_s3_client
from documind.main import create_app

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[2]


def test_readiness_reports_all_dependencies_healthy(live_settings: Settings) -> None:
    with TestClient(create_app(live_settings)) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["components"]) == {"database", "redis", "object_storage"}
    assert all(c["status"] == "ok" for c in body["components"].values())
    assert body["components"]["database"]["details"]["pgvector_version"]


def test_readiness_degrades_when_a_dependency_is_unreachable(live_settings: Settings) -> None:
    broken = live_settings.model_copy(update={"redis_url": SecretStr("redis://127.0.0.1:1/0")})
    with TestClient(create_app(broken)) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 503
    components = response.json()["components"]
    assert components["redis"]["status"] == "error"
    assert components["database"]["status"] == "ok"


async def test_pgvector_supports_cosine_distance(live_settings: Settings) -> None:
    engine = create_async_engine(live_settings.database_url.get_secret_value())
    try:
        async with engine.connect() as conn:
            distance = await conn.scalar(text("SELECT '[1,0,0]'::vector <=> '[0,1,0]'::vector"))
    finally:
        await engine.dispose()
    assert distance == pytest.approx(1.0)


async def test_redis_round_trip(live_settings: Settings) -> None:
    client = create_redis_client(live_settings)
    key = f"documind:test:{uuid.uuid4().hex}"
    try:
        await client.set(key, b"value", ex=30)
        assert await client.get(key) == b"value"
    finally:
        await client.delete(key)
        await client.aclose()


def test_bucket_bootstrap_is_idempotent_and_private(live_settings: Settings) -> None:
    client = create_s3_client(live_settings)
    bucket = f"documind-it-{uuid.uuid4().hex[:12]}"
    try:
        assert ensure_private_bucket(client, bucket, live_settings.s3_region).created is True
        assert ensure_private_bucket(client, bucket, live_settings.s3_region).created is False
    finally:
        client.delete_bucket(Bucket=bucket)
        client.close()


def test_cli_check_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["check"]) == 0
    assert '"status": "ok"' in capsys.readouterr().out


def test_migrations_upgrade_downgrade_upgrade(test_database_url: str) -> None:
    def alembic(*args: str) -> None:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "alembic", "-x", f"database_url={test_database_url}", *args],
            cwd=BACKEND_DIR,
            check=True,
            capture_output=True,
        )

    alembic("upgrade", "head")
    alembic("downgrade", "base")
    alembic("upgrade", "head")
