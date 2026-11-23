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
from documind.application.ports import (
    AccessTokenService,
    EmailSender,
    MalwareScanner,
    ObjectStorageProvider,
    PasswordHasher,
    RateLimiter,
)
from documind.core.config import Settings, mfa_key_bytes
from documind.infrastructure.cache.health import RedisHealthCheck
from documind.infrastructure.cache.redis import create_redis_client
from documind.infrastructure.database.engine import create_engine, create_session_factory
from documind.infrastructure.database.health import PostgresHealthCheck
from documind.infrastructure.email.smtp import SmtpEmailSender
from documind.infrastructure.ratelimit.redis_limiter import RedisRateLimiter
from documind.infrastructure.security.jwt_tokens import JwtAccessTokenService
from documind.infrastructure.security.malware import ClamAVScanner, NoopMalwareScanner
from documind.infrastructure.security.passwords import Argon2PasswordHasher
from documind.infrastructure.security.secret_box import SecretBox
from documind.infrastructure.storage.client import create_s3_client
from documind.infrastructure.storage.health import ObjectStorageHealthCheck
from documind.infrastructure.storage.s3_provider import S3ObjectStorage

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Container:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    s3_client: Any
    health_service: HealthService
    password_hasher: PasswordHasher
    access_tokens: AccessTokenService
    email_sender: EmailSender
    rate_limiter: RateLimiter
    storage: ObjectStorageProvider
    malware_scanner: MalwareScanner
    secret_box: SecretBox

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
        password_hasher=Argon2PasswordHasher(
            time_cost=settings.password_hash_time_cost,
            memory_kib=settings.password_hash_memory_kib,
            parallelism=settings.password_hash_parallelism,
        ),
        access_tokens=JwtAccessTokenService(
            secret=settings.jwt_secret_key.get_secret_value(),
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            ttl_seconds=settings.access_token_ttl_seconds,
        ),
        email_sender=SmtpEmailSender(settings),
        rate_limiter=RedisRateLimiter(redis, enabled=settings.rate_limit_enabled),
        storage=S3ObjectStorage(
            s3_client,
            settings.s3_bucket,
            presign_client=create_s3_client(
                settings, endpoint_url=settings.s3_public_endpoint_url or settings.s3_endpoint_url
            ),
        ),
        malware_scanner=_build_scanner(settings),
        secret_box=SecretBox(mfa_key_bytes(settings.mfa_encryption_key)),
    )


def _build_scanner(settings: Settings) -> MalwareScanner:
    if settings.malware_scanner == "clamav":
        return ClamAVScanner(
            settings.clamav_host, settings.clamav_port, settings.clamav_timeout_seconds
        )
    return NoopMalwareScanner()
