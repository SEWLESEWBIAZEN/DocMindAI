"""Composition root: builds and owns long-lived infrastructure resources.

This is the only module that wires concrete adapters to application services.
Everything else depends on abstractions or receives collaborators explicitly.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from documind.application.ai.byok import SqlWorkspaceKeys
from documind.application.ai.notifications import key_rejected_notifier
from documind.application.ai.ports import ProviderFactory
from documind.application.ai.processing import ResolvedProcessingAI
from documind.application.ai.resolver import AIResolver, CapabilityAdapter, PlatformAI
from documind.application.billing.gateway import BillingGateway
from documind.application.billing.service import CachedEntitlements
from documind.application.health_service import HealthService
from documind.application.infrastructure.resolver import InfrastructureResolver
from documind.application.ports import (
    AccessTokenService,
    CancellationRegistry,
    EmailMessage,
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
from documind.core.config import Settings, credential_key_ring, mfa_key_bytes
from documind.domain.billing.plans import Capability
from documind.infrastructure.ai.factory import ProviderAdapterFactory
from documind.infrastructure.cache.cancellation import RedisCancellationRegistry
from documind.infrastructure.cache.health import RedisHealthCheck
from documind.infrastructure.cache.redis import create_redis_client
from documind.infrastructure.database.engine import create_engine, create_session_factory
from documind.infrastructure.database.health import PostgresHealthCheck
from documind.infrastructure.database.repositories.billing import SqlUsageSink
from documind.infrastructure.database.repositories.processing import SessionEmbeddingCache
from documind.infrastructure.email.smtp import SmtpEmailSender
from documind.infrastructure.embeddings.local_provider import LocalHashingEmbeddingProvider
from documind.infrastructure.embeddings.openai_provider import OpenAIEmbeddingProvider
from documind.infrastructure.llm.anthropic_provider import AnthropicChatProvider
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
from documind.infrastructure.security.vault import EnvelopeCipher, LocalKeyRing
from documind.infrastructure.speech.openai_provider import OpenAISpeechProvider
from documind.infrastructure.storage.client import create_s3_client
from documind.infrastructure.storage.health import ObjectStorageHealthCheck
from documind.infrastructure.storage.s3_provider import S3ObjectStorage
from documind.infrastructure.tasks.celery_dispatcher import CeleryJobDispatcher
from documind.worker.celery_factory import MIGRATION_TASK, PROCESS_TASK, QUEUE, create_celery

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
    ai: AIResolver | None = None
    entitlements: CachedEntitlements | None = None
    cipher: EnvelopeCipher | None = None
    provider_factory: ProviderFactory | None = None
    workspace_keys: SqlWorkspaceKeys | None = None
    infra: InfrastructureResolver | None = None
    #: Stripe (ADR 0032); None when self-serve billing isn't configured.
    billing_gateway: BillingGateway | None = None
    #: Starts an approved infrastructure migration in the background (MI-8).
    migration_dispatch: Callable[[uuid.UUID], Awaitable[bool]] | None = None

    def infrastructure(self) -> InfrastructureResolver:
        if self.infra is None:  # pragma: no cover - always set by build_container
            raise RuntimeError("infrastructure resolver not configured")
        return self.infra

    def require_provider_factory(self) -> ProviderFactory:
        if self.provider_factory is None:  # pragma: no cover - always set by build_container
            raise RuntimeError("provider factory not configured")
        return self.provider_factory

    def ai_resolver(self) -> AIResolver:
        if self.ai is None:  # pragma: no cover - always set by build_container
            raise RuntimeError("AI resolver not configured")
        return self.ai

    async def aclose(self) -> None:
        """Release pooled connections. Called on application shutdown."""
        try:
            if self.infra is not None:
                await self.infra.aclose()
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
    session_factory = create_session_factory(engine)
    celery = create_celery(settings)
    container = Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
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
        job_dispatcher=CeleryJobDispatcher(celery, task_name=PROCESS_TASK, queue=QUEUE),
        llm_provider=_build_llm_provider(settings),
        cancellation=RedisCancellationRegistry(redis),
        image_describer=image_describer,
        **_build_speech(settings),
    )
    container.entitlements = CachedEntitlements(
        session_factory, ttl_seconds=settings.tenant_settings_cache_seconds
    )
    if settings.credential_encryption_keys is not None:
        container.cipher = EnvelopeCipher(
            LocalKeyRing(credential_key_ring(settings.credential_encryption_keys))
        )
    container.provider_factory = ProviderAdapterFactory(settings)
    container.workspace_keys = SqlWorkspaceKeys(
        session_factory,
        container.cipher,
        lambda: container.require_provider_factory(),
        notify=key_rejected_notifier(
            session_factory,
            # Resolved at call time so a swapped sender (tests) is used.
            _LateEmail(container),
            settings.public_app_url,
        ),
    )
    container.ai = AIResolver(
        lambda: platform_ai(container),
        SqlUsageSink(session_factory),
        container.entitlements,
        container.workspace_keys,
        cache_seconds=settings.tenant_settings_cache_seconds,
    )
    container.workspace_keys.invalidated.append(container.ai.invalidate)
    container.migration_dispatch = CeleryJobDispatcher(
        celery, task_name=MIGRATION_TASK, queue=QUEUE
    ).dispatch
    container.infra = InfrastructureResolver(
        session_factory,
        settings,
        container.cipher,
        container.storage,
        cache_seconds=settings.tenant_settings_cache_seconds,
    )
    container.billing_gateway = _build_billing_gateway(settings)
    return container


def _build_billing_gateway(settings: Settings) -> BillingGateway | None:
    if not settings.billing_enabled:
        return None
    from documind.infrastructure.billing.stripe_gateway import StripeGateway

    if settings.stripe_secret_key is None or settings.stripe_webhook_secret is None:
        return None  # pragma: no cover - billing_enabled checks both
    return StripeGateway(
        secret_key=settings.stripe_secret_key.get_secret_value(),
        webhook_secret=settings.stripe_webhook_secret.get_secret_value(),
        timeout_seconds=settings.stripe_timeout_seconds,
    )


def plan_changed(container: Container) -> Callable[[uuid.UUID], None]:
    """Drop cached entitlements and AI routing so a new plan applies at once."""

    def invalidate(workspace_id: uuid.UUID) -> None:
        if container.entitlements is not None:
            container.entitlements.invalidate(workspace_id)
        if container.ai is not None:
            container.ai.invalidate(workspace_id)

    return invalidate


class _LateEmail:
    def __init__(self, container: Container) -> None:
        self._c = container

    async def send(self, message: EmailMessage) -> None:
        await self._c.email_sender.send(message)


def processing_ai(c: Container) -> ResolvedProcessingAI:
    """Per-workspace embeddings and figure descriptions for background processing."""
    return ResolvedProcessingAI(
        c.ai_resolver(),
        c.session_factory,
        SessionEmbeddingCache,
        batch_size=c.settings.embedding_batch_size,
    )


def platform_ai(c: Container) -> PlatformAI:
    """The deployment's own providers, per capability, for the AI resolver."""
    s = c.settings
    adapters: dict[Capability, CapabilityAdapter] = {
        Capability.EMBEDDING: CapabilityAdapter(
            s.embedding_provider, c.embedding_provider, c.embedding_provider.model
        ),
        Capability.CHAT: CapabilityAdapter(s.llm_provider, c.llm_provider, c.llm_provider.model),
    }
    if c.image_describer is not None:
        adapters[Capability.VISION] = CapabilityAdapter(
            "local" if s.llm_provider == "local" else "openai",
            c.image_describer,
            c.image_describer.model,
        )
    if c.speech_to_text is not None:
        adapters[Capability.SPEECH_TO_TEXT] = CapabilityAdapter(
            s.speech_provider, c.speech_to_text, s.stt_model
        )
    if c.text_to_speech is not None:
        adapters[Capability.TEXT_TO_SPEECH] = CapabilityAdapter(
            s.speech_provider, c.text_to_speech, s.tts_model
        )
    return PlatformAI(adapters, s.embedding_dimensions)


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
    if settings.llm_provider == "anthropic":
        if settings.anthropic_api_key is None:  # also enforced by settings validation
            raise ValueError("ANTHROPIC_API_KEY is required for LLM_PROVIDER=anthropic")
        return AnthropicChatProvider(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.llm_model,
            base_url=settings.anthropic_base_url,
            effort=settings.anthropic_effort,
            thinking_headroom_tokens=settings.anthropic_thinking_headroom_tokens,
            refusal_fallback=settings.anthropic_refusal_fallback,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
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
        # With Claude for answers, VISION_MODEL names the OpenAI model (validated).
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
