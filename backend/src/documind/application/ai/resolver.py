"""Per-workspace AI provider resolution (ADR 0019, 0020).

For each capability the resolver decides which adapter serves the workspace
and who pays:

1. a workspace key whose provider offers the capability: used, billed to the
   workspace. If that key is marked invalid, the capability is unavailable
   (``provider_key_invalid``). There is never a silent fallback to the
   platform key;
2. otherwise the platform adapter, if the plan allows platform AI and the
   workspace hasn't chosen "use my key only";
3. otherwise unavailable (``provider_key_required``).

Every adapter handed out is wrapped for usage metering.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from documind.application.ai.metering import (
    MeteredEmbeddings,
    MeteredImageDescriber,
    MeteredLLM,
    MeteredSpeechToText,
    MeteredTextToSpeech,
    Route,
    UsageSink,
)
from documind.application.ports import (
    ChatMessage,
    EmbeddingBatch,
    EmbeddingProvider,
    ImageDescriber,
    LLMProvider,
    LLMResult,
    LLMStreamEvent,
    SpeechToTextProvider,
    TextToSpeechProvider,
)
from documind.domain.billing.plans import BilledTo, Capability, Entitlements
from documind.domain.processing.errors import ProviderAccessError

PROVIDER_KEY_REQUIRED = "provider_key_required"
PROVIDER_KEY_INVALID = "provider_key_invalid"
CAPABILITY_UNAVAILABLE = "capability_unavailable"

_MESSAGES = {
    PROVIDER_KEY_REQUIRED: (
        "This workspace uses its own AI key only, and none is set up for this feature. "
        "An admin can add one under AI providers."
    ),
    PROVIDER_KEY_INVALID: (
        "The workspace's AI provider key was rejected (revoked, expired or out of quota). "
        "An admin needs to update it under AI providers."
    ),
    CAPABILITY_UNAVAILABLE: "This AI feature isn't available in this workspace.",
}


def access_error(code: str) -> ProviderAccessError:
    return ProviderAccessError(_MESSAGES[code], code=code)


@dataclass(frozen=True, slots=True)
class CapabilityAdapter:
    provider: str
    adapter: Any
    model: str


Adapters = Mapping[Capability, CapabilityAdapter]


@dataclass(frozen=True, slots=True)
class ConfiguredProvider:
    """A workspace's provider key (secret not included)."""

    provider: str
    credential_id: uuid.UUID
    status: str  # "active" | "invalid"
    capabilities: frozenset[Capability]
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkspaceProviderConfig:
    key_only: bool = False
    providers: tuple[ConfiguredProvider, ...] = ()

    def for_capability(self, capability: Capability) -> ConfiguredProvider | None:
        for provider in self.providers:
            if capability in provider.capabilities:
                return provider
        return None


class WorkspaceProviderSource(Protocol):
    """Workspace keys (BYOK). Implemented in ``application/ai/byok.py``."""

    async def load(self, workspace_id: uuid.UUID) -> WorkspaceProviderConfig: ...

    async def adapters(self, workspace_id: uuid.UUID, provider: ConfiguredProvider) -> Adapters: ...


class NoWorkspaceKeys:
    async def load(self, workspace_id: uuid.UUID) -> WorkspaceProviderConfig:
        return WorkspaceProviderConfig()

    async def adapters(
        self, workspace_id: uuid.UUID, provider: ConfiguredProvider
    ) -> Adapters:  # pragma: no cover - never called without providers
        return {}


class EntitlementSource(Protocol):
    async def entitlements(self, workspace_id: uuid.UUID) -> Entitlements: ...


# --------------------------------------------------------------------------- #
# Stand-ins that fail with a clear, user-safe error when used
# --------------------------------------------------------------------------- #


class UnavailableEmbeddings:
    def __init__(self, code: str, *, model: str, dimensions: int) -> None:
        self.code = code
        self._model = model
        self._dimensions = dimensions

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        raise access_error(self.code)


class UnavailableLLM:
    def __init__(self, code: str, *, model: str) -> None:
        self.code = code
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def generate(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> LLMResult:
        raise access_error(self.code)

    async def stream(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> AsyncIterator[LLMStreamEvent]:
        raise access_error(self.code)
        yield  # pragma: no cover - makes this an async generator


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class WorkspaceAI:
    workspace_id: uuid.UUID
    embeddings: EmbeddingProvider
    llm: LLMProvider
    vision: ImageDescriber | None
    stt: SpeechToTextProvider | None
    tts: TextToSpeechProvider | None
    routes: dict[Capability, Route]
    unavailable: dict[Capability, str]
    entitlements: Entitlements

    def billed_to(self, capability: Capability) -> BilledTo | None:
        route = self.routes.get(capability)
        return route.billed_to if route else None

    def uses_platform(self, capability: Capability) -> bool:
        return self.billed_to(capability) is BilledTo.PLATFORM


@dataclass(frozen=True, slots=True)
class PlatformAI:
    """The deployment's own providers (``OPENAI_API_KEY`` or offline ``local``)."""

    adapters: Adapters
    embedding_dimensions: int

    @property
    def embedding_model(self) -> str:
        cap = self.adapters.get(Capability.EMBEDDING)
        return cap.model if cap else "unavailable"

    @property
    def llm_model(self) -> str:
        cap = self.adapters.get(Capability.CHAT)
        return cap.model if cap else "unavailable"


_WRAPPERS: dict[Capability, Any] = {
    Capability.EMBEDDING: MeteredEmbeddings,
    Capability.CHAT: MeteredLLM,
    Capability.VISION: MeteredImageDescriber,
}


def _wrap(capability: Capability, cap: CapabilityAdapter, route: Route, sink: UsageSink) -> Any:
    if capability is Capability.SPEECH_TO_TEXT:
        return MeteredSpeechToText(cap.adapter, route, sink, model=cap.model)
    if capability is Capability.TEXT_TO_SPEECH:
        return MeteredTextToSpeech(cap.adapter, route, sink, model=cap.model)
    return _WRAPPERS[capability](cap.adapter, route, sink)


class AIResolver:
    def __init__(
        self,
        platform: PlatformAI | Callable[[], PlatformAI],
        sink: UsageSink,
        entitlements: EntitlementSource,
        keys: WorkspaceProviderSource | None = None,
        *,
        cache_seconds: float = 5.0,
    ) -> None:
        # A callable is read on every resolution, so a container whose providers
        # are swapped (tests, reconfiguration) is picked up without rebuilding.
        self._platform_source = platform
        self._sink = sink
        self._entitlements = entitlements
        self._keys = keys or NoWorkspaceKeys()
        self._ttl = cache_seconds
        self._cache: dict[uuid.UUID, tuple[float, WorkspaceProviderConfig, Entitlements]] = {}

    @property
    def platform(self) -> PlatformAI:
        source = self._platform_source
        return source if isinstance(source, PlatformAI) else source()

    @property
    def sink(self) -> UsageSink:
        return self._sink

    def invalidate(self, workspace_id: uuid.UUID) -> None:
        self._cache.pop(workspace_id, None)

    async def _config(
        self, workspace_id: uuid.UUID
    ) -> tuple[WorkspaceProviderConfig, Entitlements]:
        now = time.monotonic()
        hit = self._cache.get(workspace_id)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1], hit[2]
        config = await self._keys.load(workspace_id)
        ents = await self._entitlements.entitlements(workspace_id)
        if len(self._cache) > 10_000:
            self._cache.clear()
        self._cache[workspace_id] = (now, config, ents)
        return config, ents

    async def resolve(
        self, workspace_id: uuid.UUID, *, user_id: uuid.UUID | None = None
    ) -> WorkspaceAI:
        config, ents = await self._config(workspace_id)
        platform_ai = self.platform
        chosen: dict[Capability, Any] = {}
        routes: dict[Capability, Route] = {}
        unavailable: dict[Capability, str] = {}
        built: dict[uuid.UUID, Adapters] = {}

        for capability in Capability:
            speech = capability in (Capability.SPEECH_TO_TEXT, Capability.TEXT_TO_SPEECH)
            if speech and not ents.server_voice:
                unavailable[capability] = CAPABILITY_UNAVAILABLE
                continue
            configured = config.for_capability(capability) if ents.byok else None
            if configured is not None:
                if configured.status != "active":
                    unavailable[capability] = PROVIDER_KEY_INVALID
                    continue
                if configured.credential_id not in built:
                    built[configured.credential_id] = await self._keys.adapters(
                        workspace_id, configured
                    )
                cap = built[configured.credential_id].get(capability)
                if cap is not None:
                    route = Route(
                        workspace_id,
                        capability,
                        cap.provider,
                        BilledTo.WORKSPACE_KEY,
                        credential_id=configured.credential_id,
                        user_id=user_id,
                    )
                    chosen[capability], routes[capability] = (
                        _wrap(capability, cap, route, self._sink),
                        route,
                    )
                    continue
            if config.key_only or not ents.platform_ai:
                unavailable[capability] = PROVIDER_KEY_REQUIRED
                continue
            platform = platform_ai.adapters.get(capability)
            if platform is None:
                unavailable[capability] = CAPABILITY_UNAVAILABLE
                continue
            route = Route(
                workspace_id, capability, platform.provider, BilledTo.PLATFORM, user_id=user_id
            )
            chosen[capability], routes[capability] = (
                _wrap(capability, platform, route, self._sink),
                route,
            )

        embeddings = chosen.get(Capability.EMBEDDING) or UnavailableEmbeddings(
            unavailable.get(Capability.EMBEDDING, CAPABILITY_UNAVAILABLE),
            model=platform_ai.embedding_model,
            dimensions=platform_ai.embedding_dimensions,
        )
        llm = chosen.get(Capability.CHAT) or UnavailableLLM(
            unavailable.get(Capability.CHAT, CAPABILITY_UNAVAILABLE),
            model=platform_ai.llm_model,
        )
        return WorkspaceAI(
            workspace_id=workspace_id,
            embeddings=embeddings,
            llm=llm,
            vision=chosen.get(Capability.VISION),
            stt=chosen.get(Capability.SPEECH_TO_TEXT),
            tts=chosen.get(Capability.TEXT_TO_SPEECH),
            routes=routes,
            unavailable=unavailable,
            entitlements=ents,
        )
