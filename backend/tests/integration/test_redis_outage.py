"""A real Redis outage for the rate limiter (production readiness F1).

The limiter is pointed at a Redis address where nothing listens, so every call
fails the way a real outage does (connection refused), through the real client.
"""

from __future__ import annotations

from typing import Any

import pytest
from redis.asyncio import Redis

from documind.infrastructure.ratelimit.redis_limiter import RedisRateLimiter
from tests.integration.conftest import Api
from tests.integration.test_processing_pipeline import ws
from tests.integration.test_rag_api import POLICY, SpyLLM, ingest

pytestmark = pytest.mark.integration


def test_rate_limits_hold_and_ai_is_refused_while_redis_is_down(make_api: Any) -> None:
    api: Api = make_api({"llm_provider": SpyLLM()}, rate_limit_login_per_email=3)
    h = api.signup("ada@example.com")
    w = ws(api, h)
    ingest(api, w, h, POLICY, "policy.md")

    container = api.client.app.state.container  # type: ignore[attr-defined]
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.5)
    container.rate_limiter = RedisRateLimiter(dead)

    # Guessing passwords: still limited (the old limiter allowed every attempt).
    codes = [
        api.client.post(
            "/api/v1/auth/login", json={"email": "ada@example.com", "password": "wrong-guess!"}
        ).status_code
        for _ in range(5)
    ]
    assert codes[:3] == [401, 401, 401]
    assert codes[3:] == [429, 429]
    assert container.rate_limiter.degraded is True

    # Answers cost money: refused until Redis is back.
    r = api.client.post(f"/api/v1/workspaces/{w}/ask", json={"question": "Who reviews?"}, headers=h)
    assert r.status_code == 503
    assert r.json()["code"] == "rate_limiter_unavailable"

    # Reading documents keeps working.
    assert api.client.get(f"/api/v1/workspaces/{w}/documents", headers=h).status_code == 200
