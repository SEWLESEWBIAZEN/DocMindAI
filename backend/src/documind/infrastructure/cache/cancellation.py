"""Cancellation flags for streaming answers, shared through Redis."""

from __future__ import annotations

import logging
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class RedisCancellationRegistry:
    def __init__(
        self, client: Redis, *, ttl_seconds: int = 900, prefix: str = "chat:cancel"
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._prefix = prefix

    def _key(self, message_id: UUID) -> str:
        return f"{self._prefix}:{message_id}"

    async def request(self, message_id: UUID) -> None:
        # Raises on Redis errors: the caller must know the cancel didn't register.
        await self._client.set(self._key(message_id), "1", ex=self._ttl)

    async def is_requested(self, message_id: UUID) -> bool:
        try:
            return bool(await self._client.exists(self._key(message_id)))
        except RedisError:
            logger.warning("cancellation check failed; continuing the stream")
            return False

    async def clear(self, message_id: UUID) -> None:
        try:
            await self._client.delete(self._key(message_id))
        except RedisError:
            logger.warning("could not clear a cancellation flag (expires on its own)")


class InMemoryCancellationRegistry:
    """Single-process registry for tests."""

    def __init__(self) -> None:
        self.requested: set[UUID] = set()

    async def request(self, message_id: UUID) -> None:
        self.requested.add(message_id)

    async def is_requested(self, message_id: UUID) -> bool:
        return message_id in self.requested

    async def clear(self, message_id: UUID) -> None:
        self.requested.discard(message_id)
