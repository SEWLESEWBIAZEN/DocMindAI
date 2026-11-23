"""Redis health check."""

from collections.abc import Mapping

from redis.asyncio import Redis

from documind.domain.health import DetailValue


class RedisHealthCheck:
    name = "redis"

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def check(self) -> Mapping[str, DetailValue]:
        await self._client.ping()
        info = await self._client.info("server")
        return {"server_version": str(info.get("redis_version", "unknown"))}
