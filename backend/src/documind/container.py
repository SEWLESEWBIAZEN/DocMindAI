"""Composition root: builds and owns long-lived infrastructure resources.

This is the only module that wires concrete adapters to application services.
Everything else depends on abstractions or receives collaborators explicitly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from documind.application.health_service import HealthService
from documind.core.config import Settings
from documind.infrastructure.cache.health import RedisHealthCheck
from documind.infrastructure.cache.redis import create_redis_client
from documind.infrastructure.database.engine import create_engine, create_session_factory
from documind.infrastructure.database.health import PostgresHealthCheck
from documind.infrastructure.storage.client import create_s3_client
from documind.infrastructure.storage.health import ObjectStorageHealthCheck

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Container:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    s3_client: Any
    health_service: HealthService

    async def aclose(self) -> None:
        """Release pooled connections. Called on application shutdown."""
        try:
            await self.redis.aclose()
        finally:
            await self.engine.dispose()
            self.s3_client.close()


def build_container(settings: Settings) -> Container:
    engine = create_engine(settings)
    redis = create_redis_client(settings)
    s3_client = create_s3_client(settings)
    health_service = HealthService(
        checks=[
            PostgresHealthCheck(engine),
            RedisHealthCheck(redis),
            ObjectStorageHealthCheck(s3_client, settings.s3_bucket),
        ],
        timeout_seconds=settings.health_check_timeout_seconds,
    )
    return Container(
        settings=settings,
        engine=engine,
        session_factory=create_session_factory(engine),
        redis=redis,
        s3_client=s3_client,
        health_service=health_service,
    )
