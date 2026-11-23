"""Redis client factory."""

from redis.asyncio import Redis

from documind.core.config import Settings


def create_redis_client(settings: Settings) -> Redis:
    """Create a pooled async Redis client. Connections are opened lazily."""
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        health_check_interval=30,
    )
