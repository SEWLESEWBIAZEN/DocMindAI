"""Fixed-window rate limiter backed by Redis."""

from __future__ import annotations

import hashlib
import logging
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from documind.domain.errors import RateLimitedError

logger = logging.getLogger(__name__)


class RedisRateLimiter:
    def __init__(self, client: Redis, *, enabled: bool = True, prefix: str = "rl") -> None:
        self._client = client
        self._enabled = enabled
        self._prefix = prefix

    async def hit(self, scope: str, identifier: str, limit: int, window_seconds: int) -> None:
        if not self._enabled:
            return
        window = int(time.time()) // window_seconds
        # Identifiers may be emails or IPs: hash them so Redis holds no PII.
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:32]
        key = f"{self._prefix}:{scope}:{digest}:{window}"
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, window_seconds, nx=True)
                count, _ = await pipe.execute()
        except RedisError:
            # Fail open: availability of login beats strict limiting during a
            # Redis outage. Argon2 cost still bounds brute-force speed.
            logger.error("Rate limiter unavailable; allowing request", extra={"scope": scope})
            return
        if int(count) > limit:
            retry_after = window_seconds - int(time.time()) % window_seconds
            raise RateLimitedError(retry_after_seconds=max(1, retry_after))
