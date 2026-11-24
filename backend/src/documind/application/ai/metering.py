"""Usage metering for AI provider calls (ADR 0020).

Every provider used for a workspace is wrapped by a ``Metered*`` adapter that
records one ``UsageEvent`` per successful call. The wrappers know who pays
(``billed_to``), which workspace and user the call is for, and optionally the
source (a message, a job) via ``usage_source``.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

from documind.application.ports import (
    ChatMessage,
    EmbeddingBatch,
    EmbeddingProvider,
    ImageDescriber,
    ImageDescription,
    LLMDelta,
    LLMProvider,
    LLMResult,
    LLMStreamEnd,
    LLMStreamEvent,
    SpeechToTextProvider,
    TextToSpeechProvider,
    Transcript,
)
from documind.domain.billing.plans import TTS_CHARS_PER_SECOND, BilledTo, Capability

logger = logging.getLogger(__name__)

#: Rough bytes per second of compressed speech (32 kbit/s Opus), used only when
#: the provider doesn't report the duration. Records say ``estimated``.
_AUDIO_BYTES_PER_SECOND = 4000


@dataclass(frozen=True, slots=True)
class UsageEvent:
    workspace_id: uuid.UUID
    capability: Capability
    provider: str
    model: str
    billed_to: BilledTo
    user_id: uuid.UUID | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    audio_seconds: float = 0.0
    characters: int = 0
    units: int = 0
    source_type: str | None = None
    source_id: str | None = None
    credential_id: uuid.UUID | None = None
    idempotency_key: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class UsageSink(Protocol):
    async def record(self, event: UsageEvent) -> None:
        """Persist the event. Must not raise: metering never breaks a request."""
        ...


class NullUsageSink:
    async def record(self, event: UsageEvent) -> None:
        return None


class MemoryUsageSink:
    """For tests."""

    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        self.events.append(event)


@dataclass(frozen=True, slots=True)
class UsageSource:
    source_type: str
    source_id: str
    idempotency_key: str | None = None


_source: ContextVar[UsageSource | None] = ContextVar("documind_usage_source", default=None)


@contextlib.contextmanager
def usage_source(
    source_type: str, source_id: object, *, idempotency_key: str | None = None
) -> Iterator[None]:
    """Attribute provider calls made inside the block to a source (message, job...)."""
    token = _source.set(UsageSource(source_type, str(source_id), idempotency_key))
    try:
        yield
    finally:
        _source.reset(token)


@dataclass(frozen=True, slots=True)
class Route:
    """Where calls for one capability go and who pays for them."""

    workspace_id: uuid.UUID
    capability: Capability
    provider: str
    billed_to: BilledTo
    credential_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None


class _Meter:
    def __init__(self, route: Route, sink: UsageSink) -> None:
        self.route = route
        self._sink = sink

    async def emit(self, model: str, **quantities: Any) -> None:
        source = _source.get()
        key = None
        if source is not None and source.idempotency_key:
            key = f"{self.route.capability.value}:{source.idempotency_key}"
        event = UsageEvent(
            workspace_id=self.route.workspace_id,
            capability=self.route.capability,
            provider=self.route.provider,
            model=model,
            billed_to=self.route.billed_to,
            user_id=self.route.user_id,
            credential_id=self.route.credential_id,
            source_type=source.source_type if source else None,
            source_id=source.source_id if source else None,
            idempotency_key=key,
            **quantities,
        )
        try:
            await self._sink.record(event)
        except Exception:  # pragma: no cover - sinks are expected not to raise
            logger.exception("usage metering failed", extra={"capability": event.capability})


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


class MeteredEmbeddings:
    def __init__(self, inner: EmbeddingProvider, route: Route, sink: UsageSink) -> None:
        self._inner = inner
        self._meter = _Meter(route, sink)

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def dimensions(self) -> int:
        return self._inner.dimensions

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        batch = await self._inner.embed(texts)
        await self._meter.emit(self._inner.model, input_tokens=batch.input_tokens, units=len(texts))
        return batch


class MeteredLLM:
    def __init__(self, inner: LLMProvider, route: Route, sink: UsageSink) -> None:
        self._inner = inner
        self._meter = _Meter(route, sink)

    @property
    def model(self) -> str:
        return self._inner.model

    async def generate(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> LLMResult:
        result = await self._inner.generate(
            messages, max_output_tokens=max_output_tokens, temperature=temperature
        )
        await self._meter.emit(
            result.model,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            units=1,
        )
        return result

    async def stream(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> AsyncIterator[LLMStreamEvent]:
        produced: list[str] = []
        ended = False
        try:
            async for event in self._inner.stream(
                messages, max_output_tokens=max_output_tokens, temperature=temperature
            ):
                if isinstance(event, LLMStreamEnd):
                    ended = True
                    await self._meter.emit(
                        event.model,
                        input_tokens=event.usage.input_tokens,
                        output_tokens=event.usage.output_tokens,
                        units=1,
                    )
                elif isinstance(event, LLMDelta):
                    produced.append(event.text)
                yield event
        finally:
            if not ended and produced:
                # Cancelled or disconnected mid-stream: the provider still bills
                # what it generated. Record an estimate rather than nothing.
                prompt = "".join(m.content for m in messages)
                await self._meter.emit(
                    self._inner.model,
                    input_tokens=_estimate_tokens(prompt),
                    output_tokens=_estimate_tokens("".join(produced)),
                    units=1,
                    details={"estimated": True, "reason": "stream_interrupted"},
                )


class MeteredImageDescriber:
    def __init__(self, inner: ImageDescriber, route: Route, sink: UsageSink) -> None:
        self._inner = inner
        self._meter = _Meter(route, sink)

    @property
    def model(self) -> str:
        return self._inner.model

    async def describe(self, image: bytes, *, media_type: str, context: str) -> ImageDescription:
        result = await self._inner.describe(image, media_type=media_type, context=context)
        await self._meter.emit(
            self._inner.model,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            units=1,
        )
        return result


class MeteredSpeechToText:
    def __init__(
        self, inner: SpeechToTextProvider, route: Route, sink: UsageSink, *, model: str
    ) -> None:
        self._inner = inner
        self._meter = _Meter(route, sink)
        self._model = model

    async def transcribe(
        self, audio: bytes, *, media_type: str, language: str | None = None
    ) -> Transcript:
        transcript = await self._inner.transcribe(audio, media_type=media_type, language=language)
        seconds = transcript.duration_seconds
        details: dict[str, Any] = {}
        if seconds is None:
            seconds = round(len(audio) / _AUDIO_BYTES_PER_SECOND, 1)
            details["estimated"] = True
        await self._meter.emit(self._model, audio_seconds=seconds, units=1, details=details)
        return transcript


class MeteredTextToSpeech:
    def __init__(
        self, inner: TextToSpeechProvider, route: Route, sink: UsageSink, *, model: str
    ) -> None:
        self._inner = inner
        self._meter = _Meter(route, sink)
        self._model = model

    async def synthesize(
        self, text: str, *, voice: str | None = None, speed: float = 1.0, audio_format: str = "mp3"
    ) -> AsyncIterator[bytes]:
        started = False
        try:
            async for chunk in self._inner.synthesize(
                text, voice=voice, speed=speed, audio_format=audio_format
            ):
                started = True
                yield chunk
        finally:
            if started:
                await self._meter.emit(
                    self._model,
                    characters=len(text),
                    audio_seconds=round(len(text) / TTS_CHARS_PER_SECOND / max(speed, 0.1), 1),
                    units=1,
                    details={"estimated": True},
                )
