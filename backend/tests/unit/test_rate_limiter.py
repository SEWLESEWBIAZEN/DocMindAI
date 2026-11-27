"""Rate limiting must not switch off when Redis is down (production readiness F1)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from documind.domain.errors import RateLimitedError, ServiceUnavailableError
from documind.infrastructure.ratelimit.redis_limiter import LocalWindowCounter, RedisRateLimiter


class FlakyRedis:
    """Counts like Redis while ``up``; raises a connection error while down."""

    def __init__(self) -> None:
        self.up = True
        self.calls = 0
        self.counts: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> Any:
        return _Pipe(self)

    async def ping(self) -> bool:
        self.calls += 1
        if not self.up:
            raise RedisConnectionError("down")
        return True


class _Pipe:
    def __init__(self, redis: FlakyRedis) -> None:
        self._r = redis
        self._key = ""

    async def __aenter__(self) -> _Pipe:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def incr(self, key: str) -> None:
        self._key = key

    def expire(self, key: str, seconds: int, nx: bool = False) -> None:
        return None

    async def execute(self) -> list[int]:
        self._r.calls += 1
        if not self._r.up:
            raise RedisConnectionError("Connection refused")
        self._r.counts[self._key] = self._r.counts.get(self._key, 0) + 1
        return [self._r.counts[self._key], 1]


def limiter(redis: FlakyRedis, **kw: Any) -> RedisRateLimiter:
    return RedisRateLimiter(redis, retry_seconds=0, **kw)  # type: ignore[arg-type]


async def test_limits_still_apply_when_redis_is_down(caplog: pytest.LogCaptureFixture) -> None:
    redis = FlakyRedis()
    rl = limiter(redis)
    await rl.hit("login:email", "ada@example.com", 3, 60)
    assert not rl.degraded

    redis.up = False
    with caplog.at_level(logging.WARNING):
        await rl.hit("login:email", "ada@example.com", 3, 60)
        await rl.hit("login:email", "ada@example.com", 3, 60)
        await rl.hit("login:email", "ada@example.com", 3, 60)
        # The fallback counts on its own: the 4th attempt in the window is refused,
        # where the old limiter let everything through.
        with pytest.raises(RateLimitedError):
            await rl.hit("login:email", "ada@example.com", 3, 60)
    assert rl.degraded
    degraded_logs = [r for r in caplog.records if "Rate limiter degraded" in r.message]
    assert len(degraded_logs) == 1  # logged once, not per request

    redis.up = True
    with caplog.at_level(logging.WARNING):
        await rl.hit("login:email", "bob@example.com", 3, 60)
    assert not rl.degraded
    assert any("recovered" in r.message for r in caplog.records)


async def test_expensive_scopes_are_refused_while_degraded() -> None:
    redis = FlakyRedis()
    redis.up = False
    rl = limiter(redis)
    with pytest.raises(ServiceUnavailableError) as caught:
        await rl.hit("ai:user", "u1", 100, 60)
    assert caught.value.code == "rate_limiter_unavailable"
    with pytest.raises(ServiceUnavailableError):
        await rl.hit("voice:user", "u1", 100, 60)
    await rl.hit("register:ip", "1.2.3.4", 100, 60)  # cheap scopes still work


async def test_a_hung_redis_is_not_asked_on_every_request() -> None:
    redis = FlakyRedis()
    redis.up = False
    rl = RedisRateLimiter(redis, retry_seconds=60)  # type: ignore[arg-type]
    for _ in range(10):
        await rl.hit("login:ip", "1.2.3.4", 100, 60)
    assert redis.calls == 1  # then the fallback until the retry interval passes


async def test_disabled_limiter_does_nothing() -> None:
    redis = FlakyRedis()
    rl = RedisRateLimiter(redis, enabled=False)  # type: ignore[arg-type]
    for _ in range(5):
        await rl.hit("login:ip", "x", 1, 60)
    assert redis.calls == 0


def test_local_counter_stays_bounded() -> None:
    counter = LocalWindowCounter(max_keys=10)
    for window in range(5):
        for who in range(8):
            counter.incr(f"rl:login:{who}:{window}")
    assert len(counter) <= 10
    assert counter.incr("rl:login:0:4") == 2  # the current window survived pruning


async def test_probe_clears_degraded_mode_without_rate_limited_traffic() -> None:
    """/metrics probes, so a process with no sign-ins stops reporting a finished outage."""
    redis = FlakyRedis()
    limiter = RedisRateLimiter(redis, retry_seconds=0.0)  # type: ignore[arg-type]
    assert await limiter.probe() is True  # healthy: nothing to do
    redis.up = False
    await limiter.hit("login:ip", "203.0.113.9", 10, 60)
    assert limiter.degraded
    assert await limiter.probe() is False
    assert limiter.degraded
    redis.up = True
    assert await limiter.probe() is True
    assert not limiter.degraded


async def test_probe_respects_the_retry_interval() -> None:
    redis = FlakyRedis()
    limiter = RedisRateLimiter(redis, retry_seconds=60.0)  # type: ignore[arg-type]
    redis.up = False
    await limiter.hit("login:ip", "203.0.113.9", 10, 60)
    calls = redis.calls
    redis.up = True
    assert await limiter.probe() is False  # asked moments ago: don't ask again yet
    assert redis.calls == calls
