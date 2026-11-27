"""Fixed-window rate limiter backed by Redis, with a safe fallback when Redis is down.

Normal operation: one counter per scope, identifier and window in Redis, shared by
every API process.

Redis unavailable (degraded mode):

* Most scopes (sign-in, password reset, 2FA codes, sign-up, API keys...) keep their
  limits with a per-process in-memory counter. Brute-force protection never switches
  off. With N API processes an attacker gets at most N times the limit, still bounded,
  and Argon2 keeps each guess slow.
* Expensive scopes (``FAIL_CLOSED_SCOPES``: AI answers and speech) are refused with
  ``503 rate_limiter_unavailable``. Per-process counters would multiply the allowance
  across instances, and these requests cost money.

While degraded, Redis is retried at most every ``retry_seconds``; other requests use
the fallback at once, so a hung Redis doesn't add its timeout to every request.
Entering and leaving degraded mode is logged once each, and ``degraded`` feeds the
``documind_rate_limiter_degraded`` metric and alert.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from documind.domain.errors import RateLimitedError, ServiceUnavailableError

logger = logging.getLogger(__name__)

#: Refused, not approximated, while Redis is down.
FAIL_CLOSED_SCOPES = frozenset({"ai:user", "voice:user"})


class LocalWindowCounter:
    """In-memory fixed windows for one process; bounded so it can't grow without limit."""

    def __init__(self, max_keys: int = 50_000) -> None:
        self._counts: dict[str, int] = {}
        self._max = max_keys
        self._lock = threading.Lock()

    def incr(self, key: str) -> int:
        with self._lock:
            if key not in self._counts and len(self._counts) >= self._max:
                self._prune()
            self._counts[key] = self._counts.get(key, 0) + 1
            return self._counts[key]

    def _prune(self) -> None:
        # Keys end with ":<window>"; drop everything but the newest window per prefix.
        newest: dict[str, int] = {}
        for key in self._counts:
            prefix, _, window = key.rpartition(":")
            newest[prefix] = max(newest.get(prefix, 0), int(window))
        self._counts = {
            k: v
            for k, v in self._counts.items()
            if int(k.rpartition(":")[2]) == newest[k.rpartition(":")[0]]
        }
        if len(self._counts) >= self._max:  # still full: forget the oldest half
            for key in list(self._counts)[: self._max // 2]:
                del self._counts[key]

    def __len__(self) -> int:
        return len(self._counts)


class RedisRateLimiter:
    def __init__(
        self,
        client: Redis,
        *,
        enabled: bool = True,
        prefix: str = "rl",
        fail_closed_scopes: frozenset[str] = FAIL_CLOSED_SCOPES,
        local: LocalWindowCounter | None = None,
        retry_seconds: float = 5.0,
    ) -> None:
        self._client = client
        self._enabled = enabled
        self._prefix = prefix
        self._fail_closed = fail_closed_scopes
        self._local = local or LocalWindowCounter()
        self._degraded_since: float | None = None
        self._retry = retry_seconds
        self._last_attempt = 0.0

    @property
    def degraded(self) -> bool:
        """True while Redis is unreachable and the fallback is in use."""
        return self._degraded_since is not None

    async def hit(self, scope: str, identifier: str, limit: int, window_seconds: int) -> None:
        if not self._enabled:
            return
        window = int(time.time()) // window_seconds
        # Identifiers may be emails or IPs: hash them so Redis holds no PII.
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:32]
        key = f"{self._prefix}:{scope}:{digest}:{window}"
        count = await self._redis_count(key, window_seconds)
        if count is None:
            if scope in self._fail_closed:
                raise ServiceUnavailableError(
                    "This is temporarily unavailable. Please try again in a minute.",
                    code="rate_limiter_unavailable",
                )
            count = self._local.incr(key)
        if int(count) > limit:
            retry_after = window_seconds - int(time.time()) % window_seconds
            raise RateLimitedError(retry_after_seconds=max(1, retry_after))

    async def probe(self) -> bool:
        """While degraded, check Redis (at most every ``retry_seconds``); True if usable.

        Recovery is otherwise noticed on the next rate-limited request, so a process
        with no such traffic would keep reporting an outage that has ended.
        """
        if not self.degraded:
            return True
        now = time.monotonic()
        if now - self._last_attempt < self._retry:
            return False
        self._last_attempt = now
        try:
            await self._client.ping()
        except (RedisError, OSError):
            return False
        self._leave_degraded()
        return True

    async def _redis_count(self, key: str, window_seconds: int) -> int | None:
        """The shared count, or None when Redis can't be used right now."""
        now = time.monotonic()
        if self.degraded and now - self._last_attempt < self._retry:
            return None
        self._last_attempt = now
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, window_seconds, nx=True)
                count, _ = await pipe.execute()
        except (RedisError, OSError):
            self._enter_degraded()
            return None
        self._leave_degraded()
        return int(count)

    def _enter_degraded(self) -> None:
        if self._degraded_since is None:
            self._degraded_since = time.monotonic()
            logger.error(
                "Rate limiter degraded: Redis unavailable. Per-process limits apply; "
                "AI and speech requests are refused until Redis is back.",
                extra={"fail_closed_scopes": sorted(self._fail_closed)},
            )

    def _leave_degraded(self) -> None:
        if self._degraded_since is not None:
            seconds = round(time.monotonic() - self._degraded_since, 1)
            self._degraded_since = None
            logger.warning(
                "Rate limiter recovered: Redis is reachable again.",
                extra={"degraded_seconds": seconds},
            )
