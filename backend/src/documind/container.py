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
    CancellationRegistry,
    EmailSender,
    EmbeddingProvider,
    ImageDescriber,
    JobDispatcher,
    LLMProvider,
    MalwareScanner,
    ObjectStorageProvider,
    PasswordHasher,
    RateLimiter,
    SpeechToTextProvider,
    TextToSpeechProvider,
)
from documind.core.config import Settings, mfa_key_bytes
from documind.infrastructure.cache.cancellation import RedisCancellationRegistry
from documind.infrastructure.cache.health import RedisHealthCheck
from documind.infrastructure.cache.redis import create_redis_client
from documind.infrastructure.database.engine import create_engine, create_session_factory
from documind.infrastructure.database.health import PostgresHealthCheck
from documind.infrastructure.email.smtp import SmtpEmailSender
from documind.infrastructure.embeddings.local_provider import LocalHashingEmbeddingProvider
from documind.infrastructure.embeddings.openai_provider import OpenAIEmbeddingProvider
from documind.infrastructure.llm.local_provider import LocalExtractiveLLM
from documind.infrastructure.llm.openai_provider import OpenAIChatProvider
from documind.infrastructure.llm.vision import LocalImageDescriber, OpenAIImageDescriber
from documind.infrastructure.processing.extractors import (
    CompositeExtractor,
    ExtractionLimits,
    FigureOptions,
    OcrOptions,
)
from documind.infrastructure.ratelimit.redis_limiter import RedisRateLimiter
from documind.infrastructure.security.jwt_tokens import JwtAccessTokenService
from documind.infrastructure.security.malware import ClamAVScanner, NoopMalwareScanner
from documind.infrastructure.security.passwords import Argon2PasswordHasher
from documind.infrastructure.security.secret_box import SecretBox
from documind.infrastructure.speech.openai_provider import OpenAISpeechProvider
from documind.infrastructure.storage.client import create_s3_client
from documind.infrastructure.storage.health import ObjectStorageHealthCheck
from documind.infrastructure.storage.s3_provider import S3ObjectStorage
from documind.infrastructure.tasks.celery_dispatcher import CeleryJobDispatcher
from documind.worker.celery_factory import PROCESS_TASK, QUEUE, create_celery

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
    extractor: CompositeExtractor
    embedding_provider: EmbeddingProvider
    job_dispatcher: JobDispatcher
    llm_provider: LLMProvider
    cancellation: CancellationRegistry
    image_describer: ImageDescriber | None = None
    speech_to_text: SpeechToTextProvider | None = None
    text_to_speech: TextToSpeechProvider | None = None

    async def aclose(self) -> None:
        """Release pooled connections. Called on application shutdown."""
        try:
            await self.redis.aclose()
        finally:
            await self.engine.dispose()
            self.s3_client.close()


def build_container(settings: Settings) -> Container:
    image_describer = _build_image_describer(settings)
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
        extractor=CompositeExtractor(
            ExtractionLimits(max_pages=settings.max_document_pages),
            OcrOptions(
                enabled=settings.ocr_enabled,
                languages=settings.ocr_languages,
                dpi=settings.ocr_dpi,
            ),
            FigureOptions(
                enabled=image_describer is not None,
                min_size_points=settings.figure_min_size_points,
                max_per_document=settings.figure_max_per_document,
            ),
        ),
        embedding_provider=_build_embedding_provider(settings),
        job_dispatcher=CeleryJobDispatcher(
            create_celery(settings), task_name=PROCESS_TASK, queue=QUEUE
        ),
        llm_provider=_build_llm_provider(settings),
        cancellation=RedisCancellationRegistry(redis),
        image_describer=image_describer,
        **_build_speech(settings),
    )


def _build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "local":
        return LocalHashingEmbeddingProvider(settings.embedding_dimensions)
    if settings.openai_api_key is None:  # also enforced by settings validation
        raise ValueError("OPENAI_API_KEY is required for EMBEDDING_PROVIDER=openai")
    return OpenAIEmbeddingProvider(
        api_key=settings.openai_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        base_url=settings.openai_base_url,
        timeout_seconds=settings.embedding_timeout_seconds,
        max_retries=settings.embedding_max_retries,
    )


def _build_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "local":
        return LocalExtractiveLLM()
    if settings.openai_api_key is None:  # also enforced by settings validation
        raise ValueError("OPENAI_API_KEY is required for LLM_PROVIDER=openai")
    return OpenAIChatProvider(
        api_key=settings.openai_api_key.get_secret_value(),
        model=settings.llm_model,
        base_url=settings.openai_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


def _build_image_describer(settings: Settings) -> ImageDescriber | None:
    if not settings.figure_descriptions_enabled or settings.figure_max_per_document == 0:
        return None
    if settings.llm_provider == "local":
        return LocalImageDescriber()
    if settings.openai_api_key is None:  # also enforced by settings validation
        raise ValueError("OPENAI_API_KEY is required for figure descriptions")
    return OpenAIImageDescriber(
        api_key=settings.openai_api_key.get_secret_value(),
        model=settings.vision_model or settings.llm_model,
        base_url=settings.openai_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
    )


def _build_speech(settings: Settings) -> dict[str, Any]:
    if settings.speech_provider != "openai" or settings.openai_api_key is None:
        return {"speech_to_text": None, "text_to_speech": None}
    provider = OpenAISpeechProvider(
        api_key=settings.openai_api_key.get_secret_value(),
        stt_model=settings.stt_model,
        tts_model=settings.tts_model,
        default_voice=settings.tts_voice,
        base_url=settings.openai_base_url,
        timeout_seconds=settings.speech_timeout_seconds,
    )
    return {"speech_to_text": provider, "text_to_speech": provider}


def _build_scanner(settings: Settings) -> MalwareScanner:
    if settings.malware_scanner == "clamav":
        return ClamAVScanner(
            settings.clamav_host, settings.clamav_port, settings.clamav_timeout_seconds
        )
    return NoopMalwareScanner()
