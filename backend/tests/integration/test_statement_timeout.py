"""PostgreSQL statement timeout for API connections (production readiness, F8)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from documind.api.errors import register_error_handlers
from documind.container import build_container
from documind.core.config import Settings
from documind.infrastructure.database.engine import create_engine

pytestmark = pytest.mark.integration


def _show(settings: Settings, role: str) -> tuple[str, str]:
    async def go() -> tuple[str, str]:
        c = build_container(settings, role=role)
        try:
            async with c.engine.connect() as conn:
                timeout = (await conn.execute(text("SHOW statement_timeout"))).scalar_one()
                name = (await conn.execute(text("SHOW application_name"))).scalar_one()
                return str(timeout), str(name)
        finally:
            await c.aclose()

    return asyncio.run(go())


def test_api_connections_have_the_timeout_and_workers_do_not(
    identity_settings: Settings,
) -> None:
    assert _show(identity_settings, "api") == ("30s", "documind-api")
    assert _show(identity_settings, "worker") == ("0", "documind-worker")
    assert _show(identity_settings, "cli") == ("0", "documind-cli")


def test_zero_turns_the_timeout_off(identity_settings: Settings) -> None:
    off = identity_settings.model_copy(update={"database_statement_timeout_seconds": 0.0})
    assert _show(off, "api")[0] == "0"


def test_a_slow_query_is_cancelled_and_answers_503(identity_settings: Settings) -> None:
    settings = identity_settings.model_copy(update={"database_statement_timeout_seconds": 0.2})
    engine = create_engine(settings)
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT pg_sleep(2)"))
        return {"finished": "yes"}

    @app.get("/broken")
    async def broken() -> dict[str, str]:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT * FROM no_such_table"))
        return {}

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/slow")
        assert r.status_code == 503
        assert r.json() == {
            "detail": "This took too long. Please try again.",
            "code": "database_timeout",
        }
        assert r.headers["retry-after"] == "5"
        # Other database errors are not disguised as timeouts.
        assert client.get("/broken").status_code == 500
    asyncio.run(engine.dispose())


def test_the_engine_raises_query_canceled(identity_settings: Settings) -> None:
    settings = identity_settings.model_copy(update={"database_statement_timeout_seconds": 0.2})

    async def go() -> None:
        engine = create_engine(settings)
        try:
            async with engine.connect() as conn:
                with pytest.raises(DBAPIError) as err:
                    await conn.execute(text("SELECT pg_sleep(2)"))
            assert getattr(err.value.orig, "sqlstate", None) == "57014"
        finally:
            await engine.dispose()

    asyncio.run(go())
