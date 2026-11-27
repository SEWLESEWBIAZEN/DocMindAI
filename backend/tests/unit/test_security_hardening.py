"""Phase 9 security hardening: log redaction, response headers, host re-checks."""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from documind.application.infrastructure.resolver import (
    InfrastructureResolver,
    InfrastructureUnavailableError,
)
from documind.core.config import Settings
from documind.core.logging import ConsoleFormatter, JsonFormatter, redact
from documind.domain.infrastructure.profiles import parse_config
from documind.main import create_app
from tests.conftest import PRODUCTION, SettingsFactory


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("Incorrect API key provided: sk-proj-abcDEF1234567890xyz", "abcDEF1234567890"),
        ("masked by the provider: sk-proj-********************7f3e", "7f3e"),
        ("worker token dmw_AbCdEfGhIjKlMnOpQrStUv", "AbCdEfGhIjKl"),
        ("api key dmk_Zq9xYwVuTsRqPoNmLkJi", "Zq9xYwVuTsRq"),
        ("scim dmscim_Zq9xYwVuTsRqPoNmLkJi", "Zq9xYwVuTsRq"),
        ("stripe said: Invalid API Key provided: sk_test_51AbCdEfGhIjKlMn", "51AbCdEfGh"),
        ("restricted rk_live_51AbCdEfGhIjKlMn", "51AbCdEfGh"),
        ("webhook secret whsec_AbCdEfGhIjKlMnOp", "AbCdEfGhIj"),
        ("slack xoxb-123456789012-abcdefghijkl", "123456789012"),
        ("refresh_token=Qm9vZ2xlUmVmcmVzaA&grant_type=refresh_token", "Qm9vZ2xl"),
        ('{"client_secret": "GOCSPX-abcdefghijkl"}', "GOCSPX-abc"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl", "eyJhbGci"),
        ("postgresql+asyncpg://dm_app:S3cr3t-Passw0rd@db.example.com:5432/docs", "S3cr3t-Passw0rd"),
        ("password=hunter2-very-secret&user=x", "hunter2"),
        ("{'secret_access_key': 'wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY'}", "wJalrXUtnFEMI"),
    ],
)
def test_redact_masks_credentials(raw: str, secret: str) -> None:
    assert secret not in redact(raw)


def test_redact_keeps_ordinary_text() -> None:
    text = (
        "Processing succeeded job_id=1234 pages=3 model=text-embedding-3-small "
        "input_tokens=812 output_tokens=120 tokens=932 sk_count=2"
    )
    assert redact(text) == text


@pytest.mark.parametrize("formatter", [JsonFormatter(), ConsoleFormatter()])
def test_formatters_redact_messages_extras_and_tracebacks(formatter: logging.Formatter) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    log = logging.getLogger("redaction-test")
    log.addHandler(handler)
    log.propagate = False
    try:
        try:
            raise RuntimeError("Incorrect API key provided: sk-proj-LEAKEDKEY123456789")
        except RuntimeError:
            log.exception("provider failed", extra={"dsn": "postgresql://u:topsecretpw@h/db"})
    finally:
        log.removeHandler(handler)
    output = stream.getvalue()
    assert "LEAKEDKEY" not in output
    assert "topsecretpw" not in output
    assert "provider failed" in output
    if isinstance(formatter, JsonFormatter):
        json.loads(output)  # still valid JSON


def test_security_headers(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health/live")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert "Strict-Transport-Security" not in response.headers  # development


def test_hsts_in_production(make_settings: SettingsFactory) -> None:
    with TestClient(
        create_app(make_settings(**PRODUCTION)), base_url="https://testserver"
    ) as client:
        response = client.get("/api/v1/health/live")
    assert response.headers["Strict-Transport-Security"].startswith("max-age=")


async def test_customer_hosts_are_rechecked_before_connecting(settings: Settings) -> None:
    resolver = InfrastructureResolver(None, settings, None, None)  # type: ignore[arg-type]
    config = parse_config(
        {
            "database": {
                "mode": "external",
                "host": "10.20.30.40",
                "database": "docs",
                "username": "documind",
                "sslmode": "disable",
            }
        }
    )
    with pytest.raises(InfrastructureUnavailableError) as exc:
        await resolver._recheck(config)
    assert exc.value.code == "infrastructure_blocked"
