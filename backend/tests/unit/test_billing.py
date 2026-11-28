"""Plans, metering and per-workspace AI resolution (ADR 0019, 0020)."""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from documind.application.ai.metering import (
    MemoryUsageSink,
    MeteredLLM,
    MeteredSpeechToText,
    MeteredTextToSpeech,
    Route,
    usage_source,
)
from documind.application.ai.resolver import (
    Adapters,
    AIResolver,
    CapabilityAdapter,
    ConfiguredProvider,
    PlatformAI,
    WorkspaceProviderConfig,
)
from documind.application.billing.service import period_bounds
from documind.application.ports import (
    ChatMessage,
    EmbeddingBatch,
    ImageDescription,
    LLMDelta,
    LLMResult,
    LLMStreamEnd,
    LLMStreamEvent,
    LLMUsage,
    Transcript,
)
from documind.application.processing.figures import FigureDescriber
from documind.domain.billing.plans import (
    PLANS,
    BilledTo,
    Capability,
    Entitlements,
    PlanKey,
)
from documind.domain.processing.errors import ProviderAccessError
from documind.domain.processing.model import Block, BlockKind, ExtractedDocument, ExtractedPage

WS = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

# --------------------------------------------------------------------------- plans


def _migration(pattern: str) -> Any:
    path = next(Path(__file__).parents[2].glob(f"alembic/versions/{pattern}"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_plans() -> list[dict[str, Any]]:
    """Plans as seeded by 0009, with the entitlements later migrations merged in."""
    plans = [
        dict(p, entitlements=dict(p["entitlements"]))
        for p in _migration("*_0009_plans_and_usage.py").PLANS
    ]
    signing = _migration("*_0024_documind_sign.py").SIGNING_ENTITLEMENTS
    for plan in plans:
        plan["entitlements"].update(signing[plan["key"]])
    return plans


def test_migration_seed_matches_plan_catalog() -> None:
    seeded = {row["key"]: row for row in _migration_plans()}
    assert set(seeded) == {p.value for p in PLANS}
    for key, plan in PLANS.items():
        assert seeded[key.value]["name"] == plan.name
        assert seeded[key.value]["entitlements"] == plan.entitlements.to_dict()


def test_byok_is_on_every_plan() -> None:
    assert all(p.entitlements.byok for p in PLANS.values())


def test_overrides_replace_known_entitlements_only() -> None:
    base = PLANS[PlanKey.FREE].entitlements
    changed = base.with_overrides({"max_members": 50, "bogus": 1, "custom_storage": True})
    assert changed.max_members == 50
    assert changed.custom_storage is True
    assert changed.max_documents == base.max_documents


def test_period_is_the_calendar_month_in_utc() -> None:
    assert period_bounds(datetime(2026, 12, 31, 23, 0, tzinfo=UTC)) == (
        datetime(2026, 12, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    )
    start, end = period_bounds(datetime(2026, 11, 24, 5, tzinfo=UTC))
    assert (start.day, end.month) == (1, 12)


# ------------------------------------------------------------------------ metering


class ScriptedLLM:
    model = "chat-model"

    def __init__(self, *, finish: bool = True) -> None:
        self.finish = finish

    async def generate(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> LLMResult:
        return LLMResult("hi", LLMUsage(10, 5), self.model)

    async def stream(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> AsyncIterator[LLMStreamEvent]:
        yield LLMDelta("Hello there, ")
        yield LLMDelta("friend.")
        if self.finish:
            yield LLMStreamEnd(LLMUsage(40, 6), self.model)


def _route(cap: Capability, billed: BilledTo = BilledTo.PLATFORM) -> Route:
    return Route(WS, cap, "openai", billed, user_id=uuid.uuid4())


async def test_generate_records_tokens_with_source() -> None:
    sink = MemoryUsageSink()
    llm = MeteredLLM(ScriptedLLM(), _route(Capability.CHAT), sink)
    with usage_source("message", "m1", idempotency_key="m1"):
        await llm.generate([ChatMessage("user", "q")], max_output_tokens=10, temperature=None)
    (event,) = sink.events
    assert (event.input_tokens, event.output_tokens, event.units) == (10, 5, 1)
    assert (event.source_type, event.source_id) == ("message", "m1")
    assert event.idempotency_key == "chat:m1"
    assert event.billed_to is BilledTo.PLATFORM


async def test_stream_records_provider_usage_at_the_end() -> None:
    sink = MemoryUsageSink()
    llm = MeteredLLM(ScriptedLLM(), _route(Capability.CHAT), sink)
    events = [
        e
        async for e in llm.stream(
            [ChatMessage("user", "q")], max_output_tokens=10, temperature=None
        )
    ]
    assert isinstance(events[-1], LLMStreamEnd)
    (event,) = sink.events
    assert (event.input_tokens, event.output_tokens) == (40, 6)
    assert "estimated" not in event.details


async def test_interrupted_stream_records_an_estimate() -> None:
    sink = MemoryUsageSink()
    llm = MeteredLLM(ScriptedLLM(), _route(Capability.CHAT), sink)
    stream = llm.stream([ChatMessage("user", "a question")], max_output_tokens=10, temperature=None)
    async for _ in stream:
        break  # client went away after the first delta
    await stream.aclose()  # type: ignore[attr-defined]
    (event,) = sink.events
    assert event.details == {"estimated": True, "reason": "stream_interrupted"}
    assert event.output_tokens >= 1


class FakeSpeech:
    def __init__(self, duration: float | None) -> None:
        self.duration = duration

    async def transcribe(
        self, audio: bytes, *, media_type: str, language: str | None = None
    ) -> Transcript:
        return Transcript("hello", duration_seconds=self.duration)

    async def synthesize(
        self, text: str, *, voice: str | None = None, speed: float = 1.0, audio_format: str = "mp3"
    ) -> AsyncIterator[bytes]:
        yield b"ID3"
        yield b"..."


async def test_speech_to_text_uses_reported_or_estimated_duration() -> None:
    sink = MemoryUsageSink()
    await MeteredSpeechToText(
        FakeSpeech(12.5), _route(Capability.SPEECH_TO_TEXT), sink, model="stt"
    ).transcribe(b"x" * 100, media_type="audio/webm")
    await MeteredSpeechToText(
        FakeSpeech(None), _route(Capability.SPEECH_TO_TEXT), sink, model="stt"
    ).transcribe(b"x" * 40_000, media_type="audio/webm")
    assert [e.audio_seconds for e in sink.events] == [12.5, 10.0]
    assert sink.events[1].details == {"estimated": True}


async def test_text_to_speech_records_characters_after_audio_starts() -> None:
    sink = MemoryUsageSink()
    tts = MeteredTextToSpeech(
        FakeSpeech(None), _route(Capability.TEXT_TO_SPEECH), sink, model="tts"
    )
    chunks = [c async for c in tts.synthesize("x" * 150)]
    assert chunks == [b"ID3", b"..."]
    (event,) = sink.events
    assert (event.characters, event.audio_seconds) == (150, 10.0)


# ------------------------------------------------------------------------ resolver


class Embed:
    def __init__(self, model: str = "platform-embed") -> None:
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return 8

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        return EmbeddingBatch([[0.0] * 8 for _ in texts], input_tokens=3 * len(texts))


def _platform(*, speech: bool = True) -> PlatformAI:
    adapters: dict[Capability, CapabilityAdapter] = {
        Capability.EMBEDDING: CapabilityAdapter("local", Embed(), "platform-embed"),
        Capability.CHAT: CapabilityAdapter("local", ScriptedLLM(), "chat-model"),
    }
    if speech:
        adapters[Capability.SPEECH_TO_TEXT] = CapabilityAdapter("openai", FakeSpeech(1.0), "stt")
        adapters[Capability.TEXT_TO_SPEECH] = CapabilityAdapter("openai", FakeSpeech(1.0), "tts")
    return PlatformAI(adapters, 8)


class Ents:
    def __init__(self, **changes: Any) -> None:
        self.value = PLANS[PlanKey.FREE].entitlements.with_overrides(changes)

    async def entitlements(self, workspace_id: uuid.UUID) -> Entitlements:
        return self.value


class Keys:
    """A workspace key that offers embeddings and chat only (no speech)."""

    def __init__(
        self, *, status: str = "active", key_only: bool = False, none: bool = False
    ) -> None:
        self.credential = uuid.uuid4()
        self.status = status
        self.key_only = key_only
        self.none = none

    async def load(self, workspace_id: uuid.UUID) -> WorkspaceProviderConfig:
        if self.none:
            return WorkspaceProviderConfig(key_only=self.key_only)
        provider = ConfiguredProvider(
            "openai",
            self.credential,
            self.status,
            frozenset({Capability.EMBEDDING, Capability.CHAT}),
        )
        return WorkspaceProviderConfig(key_only=self.key_only, providers=(provider,))

    async def adapters(self, workspace_id: uuid.UUID, provider: ConfiguredProvider) -> Adapters:
        return {
            Capability.EMBEDDING: CapabilityAdapter("openai", Embed("byok-embed"), "byok-embed"),
            Capability.CHAT: CapabilityAdapter("openai", ScriptedLLM(), "chat-model"),
        }


async def test_platform_only_routes_bill_the_platform() -> None:
    sink = MemoryUsageSink()
    ai = await AIResolver(_platform(), sink, Ents()).resolve(WS, user_id=None)
    assert ai.uses_platform(Capability.CHAT)
    assert ai.uses_platform(Capability.EMBEDDING)
    assert ai.vision is None
    assert ai.unavailable[Capability.VISION] == "capability_unavailable"
    await ai.embeddings.embed(["a", "b"])
    assert sink.events[0].input_tokens == 6


async def test_workspace_key_is_used_and_billed_to_the_workspace() -> None:
    sink = MemoryUsageSink()
    keys = Keys()
    ai = await AIResolver(_platform(), sink, Ents(), keys).resolve(WS)
    assert ai.billed_to(Capability.EMBEDDING) is BilledTo.WORKSPACE_KEY
    assert ai.embeddings.model == "byok-embed"
    await ai.llm.generate([ChatMessage("user", "q")], max_output_tokens=5, temperature=None)
    assert sink.events[0].credential_id == keys.credential
    assert sink.events[0].billed_to is BilledTo.WORKSPACE_KEY
    # The key has no speech: that capability falls back to the platform (ADR 0019 decision 2).
    assert ai.uses_platform(Capability.SPEECH_TO_TEXT)


async def test_invalid_workspace_key_never_falls_back_to_the_platform() -> None:
    ai = await AIResolver(_platform(), MemoryUsageSink(), Ents(), Keys(status="invalid")).resolve(
        WS
    )
    assert ai.unavailable[Capability.CHAT] == "provider_key_invalid"
    assert ai.billed_to(Capability.CHAT) is None
    with pytest.raises(ProviderAccessError) as exc:
        await ai.llm.generate([ChatMessage("user", "q")], max_output_tokens=5, temperature=None)
    assert exc.value.code == "provider_key_invalid"
    with pytest.raises(ProviderAccessError):
        await ai.embeddings.embed(["x"])


async def test_key_only_blocks_the_platform_for_everything_not_covered() -> None:
    ai = await AIResolver(_platform(), MemoryUsageSink(), Ents(), Keys(key_only=True)).resolve(WS)
    assert ai.billed_to(Capability.CHAT) is BilledTo.WORKSPACE_KEY
    assert ai.stt is None
    assert ai.unavailable[Capability.SPEECH_TO_TEXT] == "provider_key_required"
    none = await AIResolver(
        _platform(), MemoryUsageSink(), Ents(), Keys(key_only=True, none=True)
    ).resolve(WS)
    assert none.unavailable[Capability.EMBEDDING] == "provider_key_required"


async def test_plan_without_platform_ai_requires_a_key() -> None:
    ai = await AIResolver(
        _platform(), MemoryUsageSink(), Ents(platform_ai=False), Keys(none=True)
    ).resolve(WS)
    assert ai.unavailable[Capability.CHAT] == "provider_key_required"


async def test_server_voice_entitlement_gates_speech() -> None:
    ai = await AIResolver(_platform(), MemoryUsageSink(), Ents(server_voice=False)).resolve(WS)
    assert ai.stt is None
    assert ai.tts is None


async def test_platform_source_is_read_on_every_resolution() -> None:
    current = {"p": _platform(speech=False)}
    resolver = AIResolver(lambda: current["p"], MemoryUsageSink(), Ents())
    assert (await resolver.resolve(WS)).stt is None
    current["p"] = _platform()
    assert (await resolver.resolve(WS)).stt is not None


# ------------------------------------------------------------------- figure quota


class Describer:
    model = "vision"

    async def describe(self, image: bytes, *, media_type: str, context: str) -> ImageDescription:
        return ImageDescription("A chart.")


async def test_figure_descriptions_stop_at_the_remaining_quota() -> None:
    figure = Block(BlockKind.FIGURE, "", image=b"\x89PNG")
    page = ExtractedPage(position=0, page_number=1, blocks=(figure, figure, figure))
    doc = ExtractedDocument(title="t", pages=(page,))
    result, described, failed = await FigureDescriber(Describer(), limit=2).describe(doc)
    assert (described, failed) == (2, 0)
    assert "figures_not_described:quota=1" in result.warnings
