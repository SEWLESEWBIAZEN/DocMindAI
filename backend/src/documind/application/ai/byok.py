"""Bring your own API key (ADR 0019, 0022).

* ``SqlWorkspaceKeys`` resolves a workspace's keys for the AI resolver (API and
  worker), decrypting only in memory and caching built adapters briefly.
* Guarded adapters mark a key ``invalid`` when the provider rejects it at
  runtime, notify the workspace's admins, and fail with
  ``provider_key_invalid``. There is never a fallback to the platform key.
* ``ProviderKeyService`` is the management use case: 2FA step-up, validation,
  audit, re-indexing when the embedding model changes.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.ai.ports import ProviderFactory
from documind.application.ai.resolver import (
    PROVIDER_KEY_INVALID,
    Adapters,
    AIResolver,
    CapabilityAdapter,
    ConfiguredProvider,
    WorkspaceProviderConfig,
    access_error,
)
from documind.application.billing.service import BillingService
from documind.application.credentials.vault import CredentialVault
from documind.application.identity.common import AuditRecorder, RequestMeta, utcnow
from documind.application.ports import (
    ChatMessage,
    EmbeddingBatch,
    EmbeddingProvider,
    ImageDescriber,
    ImageDescription,
    LLMProvider,
    LLMResult,
    LLMStreamEvent,
    SpeechToTextProvider,
    TextToSpeechProvider,
    Transcript,
)
from documind.application.processing.reindex import ReindexEstimate, ReindexService
from documind.core.config import Settings
from documind.domain.ai.providers import PROVIDERS, ProviderSpec, clean_settings
from documind.domain.billing.plans import Capability
from documind.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext, ensure_recent_mfa
from documind.domain.processing.errors import ProcessingError
from documind.infrastructure.database.models import (
    Credential,
    WorkspaceAIProvider,
    WorkspaceAISettings,
)
from documind.infrastructure.database.repositories.identity import AuditLogRepository
from documind.infrastructure.security.vault import EnvelopeCipher, VaultError

logger = logging.getLogger(__name__)

#: Provider error codes that mean "this key no longer works" (not a blip).
KEY_FAILURE_CODES = frozenset(
    {"llm_misconfigured", "embedding_misconfigured", "provider_quota_exhausted"}
)

OnInvalid = Callable[[uuid.UUID, uuid.UUID, str], Awaitable[None]]


# --------------------------------------------------------------------------- #
# Guarded adapters
# --------------------------------------------------------------------------- #


class _Guard:
    def __init__(self, workspace_id: uuid.UUID, credential_id: uuid.UUID, on_invalid: OnInvalid):
        self._ws = workspace_id
        self._cred = credential_id
        self._on_invalid = on_invalid

    async def check(self, exc: ProcessingError) -> None:
        if exc.code in KEY_FAILURE_CODES:
            await self._on_invalid(self._ws, self._cred, exc.code)
            raise access_error(PROVIDER_KEY_INVALID) from exc


class GuardedEmbeddings:
    def __init__(self, inner: EmbeddingProvider, guard: _Guard) -> None:
        self._inner, self._guard = inner, guard

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def dimensions(self) -> int:
        return self._inner.dimensions

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        try:
            return await self._inner.embed(texts)
        except ProcessingError as exc:
            await self._guard.check(exc)
            raise


class GuardedLLM:
    def __init__(self, inner: LLMProvider, guard: _Guard) -> None:
        self._inner, self._guard = inner, guard

    @property
    def model(self) -> str:
        return self._inner.model

    async def generate(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> LLMResult:
        try:
            return await self._inner.generate(
                messages, max_output_tokens=max_output_tokens, temperature=temperature
            )
        except ProcessingError as exc:
            await self._guard.check(exc)
            raise

    async def stream(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> AsyncIterator[LLMStreamEvent]:
        try:
            async for event in self._inner.stream(
                messages, max_output_tokens=max_output_tokens, temperature=temperature
            ):
                yield event
        except ProcessingError as exc:
            await self._guard.check(exc)
            raise


class GuardedVision:
    def __init__(self, inner: ImageDescriber, guard: _Guard) -> None:
        self._inner, self._guard = inner, guard

    @property
    def model(self) -> str:
        return self._inner.model

    async def describe(self, image: bytes, *, media_type: str, context: str) -> ImageDescription:
        try:
            return await self._inner.describe(image, media_type=media_type, context=context)
        except ProcessingError as exc:
            await self._guard.check(exc)
            raise


class GuardedSpeech:
    def __init__(self, stt: SpeechToTextProvider, tts: TextToSpeechProvider, guard: _Guard) -> None:
        self._stt, self._tts, self._guard = stt, tts, guard

    async def transcribe(
        self, audio: bytes, *, media_type: str, language: str | None = None
    ) -> Transcript:
        try:
            return await self._stt.transcribe(audio, media_type=media_type, language=language)
        except ProcessingError as exc:
            await self._guard.check(exc)
            raise

    async def synthesize(
        self, text: str, *, voice: str | None = None, speed: float = 1.0, audio_format: str = "mp3"
    ) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._tts.synthesize(
                text, voice=voice, speed=speed, audio_format=audio_format
            ):
                yield chunk
        except ProcessingError as exc:
            await self._guard.check(exc)
            raise


def guard_adapters(adapters: Adapters, guard: _Guard) -> dict[Capability, CapabilityAdapter]:
    out: dict[Capability, CapabilityAdapter] = {}
    speech: GuardedSpeech | None = None
    stt, tts = adapters.get(Capability.SPEECH_TO_TEXT), adapters.get(Capability.TEXT_TO_SPEECH)
    if stt is not None and tts is not None:
        speech = GuardedSpeech(stt.adapter, tts.adapter, guard)
    for cap, item in adapters.items():
        wrapped: Any
        if cap is Capability.EMBEDDING:
            wrapped = GuardedEmbeddings(item.adapter, guard)
        elif cap is Capability.CHAT:
            wrapped = GuardedLLM(item.adapter, guard)
        elif cap is Capability.VISION:
            wrapped = GuardedVision(item.adapter, guard)
        elif speech is not None:
            wrapped = speech
        else:  # pragma: no cover - speech adapters come in pairs
            wrapped = item.adapter
        out[cap] = CapabilityAdapter(item.provider, wrapped, item.model)
    return out


# --------------------------------------------------------------------------- #
# Resolver source
# --------------------------------------------------------------------------- #


class SqlWorkspaceKeys:
    """``WorkspaceProviderSource`` backed by the control-plane database."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: EnvelopeCipher | None,
        factory: Callable[[], ProviderFactory],
        *,
        cache_seconds: float = 300.0,
        notify: OnInvalid | None = None,
    ) -> None:
        self._sessions = session_factory
        self._cipher = cipher
        self._factory_source = factory
        self._ttl = cache_seconds
        self._notify = notify
        self._adapters: dict[uuid.UUID, tuple[float, Adapters]] = {}
        self.invalidated: list[Callable[[uuid.UUID], None]] = []

    @property
    def factory(self) -> ProviderFactory:
        """Read on every use, so a swapped factory (tests, reconfiguration) applies."""
        return self._factory_source()

    def forget(self, credential_id: uuid.UUID) -> None:
        self._adapters.pop(credential_id, None)

    async def load(self, workspace_id: uuid.UUID) -> WorkspaceProviderConfig:
        async with self._sessions() as session:
            settings = await session.get(WorkspaceAISettings, workspace_id)
            rows = (
                await session.execute(
                    select(WorkspaceAIProvider)
                    .where(WorkspaceAIProvider.workspace_id == workspace_id)
                    .order_by(WorkspaceAIProvider.created_at)
                )
            ).scalars()
            providers = tuple(
                ConfiguredProvider(
                    row.provider,
                    row.credential_id,
                    row.status,
                    frozenset(Capability(c) for c in row.capabilities),
                    dict(row.settings),
                )
                for row in rows
                if PROVIDERS.get(row.provider) is not None
            )
        return WorkspaceProviderConfig(
            key_only=bool(settings and settings.key_only), providers=providers
        )

    async def adapters(self, workspace_id: uuid.UUID, provider: ConfiguredProvider) -> Adapters:
        now = time.monotonic()
        hit = self._adapters.get(provider.credential_id)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        async with self._sessions() as session:
            vault = CredentialVault(session, self._cipher)
            row = await vault.get(workspace_id, provider.credential_id)
            if row is None:
                raise access_error(PROVIDER_KEY_INVALID)
            try:
                secret = vault.reveal(row)
            except VaultError as exc:
                logger.error(
                    "workspace credential could not be decrypted",
                    extra={"workspace_id": str(workspace_id), "credential_id": str(row.id)},
                )
                raise access_error(PROVIDER_KEY_INVALID) from exc
            await session.commit()  # last_used_at
        built = self.factory.build(provider.provider, secret, dict(provider.settings))
        del secret
        guarded = guard_adapters(
            built, _Guard(workspace_id, provider.credential_id, self.mark_invalid)
        )
        if len(self._adapters) > 5_000:
            self._adapters.clear()
        self._adapters[provider.credential_id] = (now, guarded)
        return guarded

    async def mark_invalid(
        self, workspace_id: uuid.UUID, credential_id: uuid.UUID, code: str
    ) -> None:
        """The provider rejected the key: stop using it until an admin fixes it."""
        self.forget(credential_id)
        async with self._sessions() as session:
            result = await session.execute(
                update(WorkspaceAIProvider)
                .where(
                    WorkspaceAIProvider.workspace_id == workspace_id,
                    WorkspaceAIProvider.credential_id == credential_id,
                    WorkspaceAIProvider.status == "active",
                )
                .values(status="invalid", last_error_code=code, invalid_since=utcnow())
                .returning(WorkspaceAIProvider.provider)
            )
            provider = result.scalar_one_or_none()
            if provider is not None:
                AuditRecorder(AuditLogRepository(session)).record(
                    "ai_provider.key_rejected",
                    actor_user_id=None,
                    workspace_id=workspace_id,
                    target_type="ai_provider",
                    target_id=provider,
                    details={"provider": provider, "code": code},
                )
            await session.commit()
        for callback in self.invalidated:
            callback(workspace_id)
        if provider is not None:
            logger.warning(
                "workspace AI key rejected by provider",
                extra={"workspace_id": str(workspace_id), "provider": provider, "code": code},
            )
            if self._notify is not None:
                try:
                    await self._notify(workspace_id, credential_id, provider)
                except Exception:
                    logger.exception("failed to notify admins about a rejected key")


# --------------------------------------------------------------------------- #
# Management use case
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProviderView:
    provider: str
    name: str
    status: str
    hint: str | None
    capabilities: tuple[str, ...]
    settings: dict[str, str]
    models: dict[str, str]
    last_error_code: str | None
    last_validated_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    created_by_user_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class AIProvidersOverview:
    byok_allowed: bool
    platform_ai_allowed: bool
    key_only: bool
    providers: list[ProviderView]
    catalog: list[ProviderSpec]
    effective: dict[str, str]  # capability -> "workspace_key" | "platform" | unavailable code
    embedding_model: str


class ProviderKeyService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        cipher: EnvelopeCipher | None,
        factory: ProviderFactory,
        resolver: AIResolver,
        keys: SqlWorkspaceKeys | None,
        meta: RequestMeta,
        *,
        data_session: AsyncSession | None = None,
    ) -> None:
        self._s = session
        self._data = data_session or session
        self._settings = settings
        self._vault = CredentialVault(session, cipher)
        self._factory = factory
        self._resolver = resolver
        self._keys = keys
        self._audit = AuditRecorder(AuditLogRepository(session), meta)
        self.queued_jobs: list[uuid.UUID] = []

    # ------------------------------------------------------------- helpers

    def _require_manage(self, ctx: WorkspaceContext) -> None:
        ctx.require(Permission.AI_PROVIDERS_MANAGE)

    def _step_up(self, ctx: WorkspaceContext) -> None:
        ensure_recent_mfa(ctx, utcnow(), self._settings.mfa_step_up_ttl_seconds)

    async def _rows(self, workspace_id: uuid.UUID) -> list[WorkspaceAIProvider]:
        return list(
            (
                await self._s.execute(
                    select(WorkspaceAIProvider)
                    .where(WorkspaceAIProvider.workspace_id == workspace_id)
                    .order_by(WorkspaceAIProvider.created_at)
                )
            ).scalars()
        )

    async def _row(
        self, workspace_id: uuid.UUID, provider: str, *, lock: bool = False
    ) -> WorkspaceAIProvider | None:
        stmt = select(WorkspaceAIProvider).where(
            WorkspaceAIProvider.workspace_id == workspace_id,
            WorkspaceAIProvider.provider == provider,
        )
        if lock:
            stmt = stmt.with_for_update()
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def _settings_row(self, workspace_id: uuid.UUID) -> WorkspaceAISettings:
        row = await self._s.get(WorkspaceAISettings, workspace_id, with_for_update=True)
        if row is None:
            row = WorkspaceAISettings(workspace_id=workspace_id, key_only=False)
            self._s.add(row)
        return row

    def _spec(self, provider: str) -> ProviderSpec:
        spec = PROVIDERS.get(provider)
        if spec is None:
            raise NotFoundError("Unknown AI provider.")
        if spec.status != "available":
            raise ValidationFailedError(
                f"{spec.name} isn't available yet. OpenAI is supported today.",
                code="provider_not_available",
            )
        return spec

    def _invalidate(self, workspace_id: uuid.UUID, *credential_ids: uuid.UUID) -> None:
        self._resolver.invalidate(workspace_id)
        if self._keys is not None:
            for cid in credential_ids:
                self._keys.forget(cid)

    async def _embedding_change(
        self, ctx: WorkspaceContext, target_model: str, reindex: bool
    ) -> ReindexEstimate:
        estimate = await ReindexService(self._data, self._settings).estimate(
            ctx.workspace_id, target_model
        )
        if estimate.required and not reindex:
            raise ConflictError(
                f"This changes the embedding model to {target_model}. {estimate.documents} "
                f"document(s) (about {estimate.tokens:,} tokens) must be re-indexed before search "
                "finds them again. Confirm to re-index.",
                code="reindex_required",
            )
        return estimate

    async def _queue_reindex(self, ctx: WorkspaceContext, target_model: str) -> None:
        self.queued_jobs = await ReindexService(self._data, self._settings).queue(
            ctx.workspace_id, target_model, requested_by=ctx.user_id
        )

    async def _credential(self, row: WorkspaceAIProvider) -> Credential | None:
        return await self._vault.get(row.workspace_id, row.credential_id)

    # --------------------------------------------------------------- reads

    async def overview(self, ctx: WorkspaceContext) -> AIProvidersOverview:
        self._require_manage(ctx)
        ents = await BillingService(self._s).entitlements(ctx.workspace_id)
        settings = await self._s.get(WorkspaceAISettings, ctx.workspace_id)
        views = []
        for row in await self._rows(ctx.workspace_id):
            spec = PROVIDERS.get(row.provider)
            cred = await self._credential(row)
            models = (
                {c.value: m for c, m in self._factory.models(row.provider, row.settings).items()}
                if spec is not None and spec.status == "available"
                else {}
            )
            views.append(
                ProviderView(
                    provider=row.provider,
                    name=spec.name if spec else row.provider,
                    status=row.status,
                    hint=cred.hint if cred else None,
                    capabilities=tuple(row.capabilities),
                    settings=dict(row.settings),
                    models=models,
                    last_error_code=row.last_error_code,
                    last_validated_at=row.last_validated_at,
                    last_used_at=cred.last_used_at if cred else None,
                    created_at=row.created_at,
                    created_by_user_id=row.created_by_user_id,
                )
            )
        self._resolver.invalidate(ctx.workspace_id)
        ai = await self._resolver.resolve(ctx.workspace_id)
        effective = {
            cap.value: (
                ai.routes[cap].billed_to.value
                if cap in ai.routes
                else ai.unavailable.get(cap, "capability_unavailable")
            )
            for cap in Capability
        }
        return AIProvidersOverview(
            byok_allowed=ents.byok,
            platform_ai_allowed=ents.platform_ai,
            key_only=bool(settings and settings.key_only),
            providers=views,
            catalog=list(PROVIDERS.values()),
            effective=effective,
            embedding_model=ai.embeddings.model,
        )

    async def reindex_estimate(self, ctx: WorkspaceContext) -> ReindexEstimate:
        self._require_manage(ctx)
        ai = await self._resolver.resolve(ctx.workspace_id)
        return await ReindexService(self._data, self._settings).estimate(
            ctx.workspace_id, ai.embeddings.model
        )

    # -------------------------------------------------------------- writes

    async def put_key(
        self,
        ctx: WorkspaceContext,
        provider: str,
        api_key: str,
        options: dict[str, object] | None,
        *,
        reindex: bool = False,
    ) -> ProviderView:
        """Add or replace a key. Requires a fresh 2FA step-up; the key is validated first."""
        self._require_manage(ctx)
        self._step_up(ctx)
        spec = self._spec(provider)
        ents = await BillingService(self._s).entitlements(ctx.workspace_id)
        if not ents.byok:
            raise PermissionDeniedError(
                "Your plan doesn't include using your own AI key.", code="byok_not_in_plan"
            )
        api_key = api_key.strip()
        if not 20 <= len(api_key) <= 512 or any(ch.isspace() for ch in api_key):
            raise ValidationFailedError(
                "That doesn't look like an API key.", code="invalid_key_format"
            )
        try:
            cleaned = clean_settings(spec, options)
        except ValueError as exc:
            raise ValidationFailedError(str(exc)) from exc

        result = await self._factory.validate(provider, api_key, cleaned)
        if not result.ok:
            self._audit.record(
                "ai_provider.validation_failed",
                actor_user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                target_type="ai_provider",
                target_id=provider,
                details={"provider": provider, "code": result.code},
            )
            await self._s.commit()
            raise ValidationFailedError(
                result.message or "The key couldn't be verified.",
                code=result.code or "provider_validation_failed",
            )

        target_model = self._factory.embedding_model(provider, cleaned)
        await self._embedding_change(ctx, target_model, reindex)

        row = await self._row(ctx.workspace_id, provider, lock=True)
        replaced = row is not None
        old_credential: uuid.UUID | None = None
        credential = self._vault.store(
            ctx.workspace_id, "ai_provider_key", api_key, created_by=ctx.user_id
        )
        del api_key
        await self._s.flush()  # the credential row must exist before it is referenced
        if row is None:
            row = WorkspaceAIProvider(
                workspace_id=ctx.workspace_id,
                provider=provider,
                credential_id=credential.id,
                created_by_user_id=ctx.user_id,
            )
            self._s.add(row)
        else:
            old_credential = row.credential_id
            old = await self._vault.get(ctx.workspace_id, old_credential)
            if old is not None:
                self._vault.revoke(old)
        row.credential_id = credential.id
        row.capabilities = sorted(c.value for c in spec.capabilities)
        row.settings = cleaned
        row.status = "active"
        row.last_error_code = None
        row.invalid_since = None
        row.last_validated_at = utcnow()
        self._audit.record(
            "ai_provider.key_replaced" if replaced else "ai_provider.key_added",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="ai_provider",
            target_id=provider,
            details={
                "provider": provider,
                "hint": credential.hint,
                "settings": cleaned,
                "embedding_model": target_model,
                "reindex": reindex,
            },
        )
        if reindex:
            await self._queue_reindex(ctx, target_model)
        await self._s.commit()
        if self._data is not self._s:
            await self._data.commit()
        self._invalidate(ctx.workspace_id, *(c for c in (old_credential,) if c))
        views = {v.provider: v for v in (await self.overview(ctx)).providers}
        return views[provider]

    async def remove_key(
        self, ctx: WorkspaceContext, provider: str, *, reindex: bool = False
    ) -> None:
        self._require_manage(ctx)
        self._step_up(ctx)
        row = await self._row(ctx.workspace_id, provider, lock=True)
        if row is None:
            raise NotFoundError("No key is saved for this provider.")
        settings = await self._s.get(WorkspaceAISettings, ctx.workspace_id)
        platform_model = self._resolver.platform.embedding_model
        if not (settings and settings.key_only) and Capability.EMBEDDING.value in row.capabilities:
            await self._embedding_change(ctx, platform_model, reindex)
        cred = await self._credential(row)
        credential_id = row.credential_id
        await self._s.delete(row)
        await self._s.flush()
        if cred is not None:
            self._vault.revoke(cred)
        self._audit.record(
            "ai_provider.key_removed",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="ai_provider",
            target_id=provider,
            details={"provider": provider, "hint": cred.hint if cred else None, "reindex": reindex},
        )
        if reindex:
            await self._queue_reindex(ctx, platform_model)
        await self._s.commit()
        if self._data is not self._s:
            await self._data.commit()
        self._invalidate(ctx.workspace_id, credential_id)

    async def revalidate(self, ctx: WorkspaceContext, provider: str) -> ProviderView:
        """Check a saved key again (e.g. after topping up the provider account)."""
        self._require_manage(ctx)
        row = await self._row(ctx.workspace_id, provider, lock=True)
        if row is None:
            raise NotFoundError("No key is saved for this provider.")
        cred = await self._credential(row)
        if cred is None:
            raise NotFoundError("No key is saved for this provider.")
        try:
            secret = self._vault.reveal(cred)
        except VaultError as exc:
            raise ConflictError(
                "The saved key can't be read. Enter it again.", code="credential_unreadable"
            ) from exc
        result = await self._factory.validate(provider, secret, dict(row.settings))
        del secret
        if result.ok:
            row.status, row.last_error_code, row.invalid_since = "active", None, None
            row.last_validated_at = utcnow()
        else:
            row.status, row.last_error_code = "invalid", result.code
            row.invalid_since = row.invalid_since or utcnow()
        self._audit.record(
            "ai_provider.revalidated",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="ai_provider",
            target_id=provider,
            details={"provider": provider, "ok": result.ok, "code": result.code},
        )
        await self._s.commit()
        self._invalidate(ctx.workspace_id, row.credential_id)
        if not result.ok:
            raise ValidationFailedError(
                result.message or "The key couldn't be verified.",
                code=result.code or "provider_validation_failed",
            )
        views = {v.provider: v for v in (await self.overview(ctx)).providers}
        return views[provider]

    async def set_key_only(self, ctx: WorkspaceContext, key_only: bool) -> bool:
        """Turning "use my key only" off allows platform usage again, so it needs 2FA."""
        self._require_manage(ctx)
        row = await self._settings_row(ctx.workspace_id)
        if row.key_only == key_only:
            return key_only
        if not key_only:
            self._step_up(ctx)
        row.key_only = key_only
        row.updated_by_user_id = ctx.user_id
        self._audit.record(
            "ai_provider.key_only_changed",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="workspace",
            target_id=ctx.workspace_id,
            details={"key_only": key_only},
        )
        await self._s.commit()
        self._invalidate(ctx.workspace_id)
        return key_only

    async def start_reindex(self, ctx: WorkspaceContext) -> tuple[ReindexEstimate, int]:
        """Re-index documents that aren't embedded with the current model."""
        self._require_manage(ctx)
        ai = await self._resolver.resolve(ctx.workspace_id)
        estimate = await ReindexService(self._data, self._settings).estimate(
            ctx.workspace_id, ai.embeddings.model
        )
        await self._queue_reindex(ctx, ai.embeddings.model)
        self._audit.record(
            "workspace.reindex_started",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="workspace",
            target_id=ctx.workspace_id,
            details={"embedding_model": ai.embeddings.model, "documents": len(self.queued_jobs)},
        )
        await self._s.commit()
        if self._data is not self._s:
            await self._data.commit()
        return estimate, len(self.queued_jobs)
