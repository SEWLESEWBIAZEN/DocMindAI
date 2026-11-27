"""Live checks against the real OpenAI API (opt-in, small and cheap).

Run:  OPENAI_API_KEY=... LIVE_OPENAI_CHAT_MODEL=<a chat model your key can use> \
      uv run pytest -m live tests/live/test_openai_live.py -v

Every test uses DocuMind's own adapters, so what's verified is the code that ships.
Costs: a few hundred tokens in total (well under one US cent with small models).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import openai
import pytest

from documind.application.ports import ChatMessage, LLMDelta, LLMStreamEnd
from documind.core.config import Settings
from documind.domain.processing.errors import PermanentProcessingError, TransientProcessingError
from documind.infrastructure.ai.factory import ProviderAdapterFactory
from documind.infrastructure.embeddings.openai_provider import OpenAIEmbeddingProvider
from documind.infrastructure.llm.openai_provider import OpenAIChatProvider
from documind.infrastructure.llm.vision import OpenAIImageDescriber
from documind.infrastructure.speech.openai_provider import OpenAISpeechProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not set"),
]

KEY = os.environ.get("OPENAI_API_KEY", "")
CHAT = os.environ.get("LIVE_OPENAI_CHAT_MODEL", "gpt-5.4-mini")
EMBED = os.environ.get("LIVE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
VISION = os.environ.get("LIVE_OPENAI_VISION_MODEL", CHAT)
STT = os.environ.get("LIVE_OPENAI_STT_MODEL", "gpt-4o-mini-transcribe")
TTS = os.environ.get("LIVE_OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
#: A model name that can't exist, to see how "no such model" is reported.
MISSING = "documind-nonexistent-model"

SAY = [ChatMessage("user", "Reply with exactly the word: pong")]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def chat(model: str = CHAT, **kw: Any) -> OpenAIChatProvider:
    return OpenAIChatProvider(api_key=KEY, model=model, **{"max_retries": 1, **kw})


# --------------------------------------------------------------------------- key & models


def test_models_visible_to_the_key() -> None:
    async def go() -> list[str]:
        client = openai.AsyncOpenAI(api_key=KEY, timeout=20, max_retries=1)
        try:
            return [m.id async for m in client.models.list()]
        finally:
            await client.close()

    ids = run(go())
    print("models visible to this key:", sorted(ids))
    assert ids


def test_byok_validation_reports_usable_and_unusable_models() -> None:
    factory = ProviderAdapterFactory(Settings())

    ok = run(factory.validate("openai", KEY, {"chat_model": CHAT, "embedding_model": EMBED}))
    print(f"validate(chat={CHAT}, embedding={EMBED}) -> {ok}")
    assert ok.ok is True
    bad = run(factory.validate("openai", KEY, {"chat_model": MISSING, "embedding_model": EMBED}))
    print(f"validate(chat={MISSING}) -> {bad}")
    assert bad.ok is False
    assert bad.code == "model_not_available"


# --------------------------------------------------------------------------- chat


def test_chat_completion_with_usage() -> None:
    result = run(chat().generate(SAY, max_output_tokens=200, temperature=None))
    print("chat:", result.model, repr(result.text[:80]), result.usage, result.finish_reason)
    assert result.text.strip()
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0


def test_streaming_yields_deltas_then_usage() -> None:
    async def go() -> tuple[list[str], LLMStreamEnd | None]:
        deltas: list[str] = []
        end: LLMStreamEnd | None = None
        async for event in chat().stream(SAY, max_output_tokens=200, temperature=None):
            if isinstance(event, LLMDelta):
                deltas.append(event.text)
            elif isinstance(event, LLMStreamEnd):
                end = event
        return deltas, end

    deltas, end = run(go())
    print("stream:", len(deltas), "deltas;", end)
    assert deltas
    assert end is not None
    assert end.usage.output_tokens > 0


def test_cancelling_a_stream_closes_it_cleanly() -> None:
    async def go() -> int:
        seen = 0
        gen = chat().stream(
            [ChatMessage("user", "Count from 1 to 50, one number per line.")],
            max_output_tokens=400,
            temperature=None,
        )
        async for event in gen:
            if isinstance(event, LLMDelta):
                seen += 1
                if seen >= 2:
                    break
        await gen.aclose()  # what the reply runner does when the browser disconnects
        return seen

    assert run(go()) >= 2


def test_unknown_model_is_reported_as_a_configuration_error() -> None:
    with pytest.raises((TransientProcessingError, PermanentProcessingError)) as caught:
        run(chat(MISSING).generate(SAY, max_output_tokens=50, temperature=None))
    print("unknown model ->", type(caught.value).__name__, caught.value.code, caught.value)
    assert caught.value.code == "llm_misconfigured"


def test_timeout_is_bounded_and_classified() -> None:
    started = time.monotonic()
    with pytest.raises(TransientProcessingError) as caught:
        run(
            chat(timeout_seconds=0.01, max_retries=0).generate(
                SAY, max_output_tokens=50, temperature=None
            )
        )
    took = time.monotonic() - started
    print("timeout ->", caught.value.code, f"after {took:.2f}s")
    assert caught.value.code == "llm_unavailable"
    assert took < 10


# --------------------------------------------------------------------------- embeddings


def test_embeddings_have_the_configured_dimensions() -> None:
    provider = OpenAIEmbeddingProvider(
        api_key=KEY, model=EMBED, dimensions=1536, timeout_seconds=30, max_retries=1
    )
    batch = run(provider.embed(["DocuMind verification", "second input"]))
    print(
        "embeddings:", len(batch.vectors), "x", len(batch.vectors[0]), "tokens", batch.input_tokens
    )
    assert len(batch.vectors) == 2
    assert all(len(v) == 1536 for v in batch.vectors)


# --------------------------------------------------------------------------- vision & speech


def _png() -> bytes:
    import fitz  # PyMuPDF draws a small bar chart without extra dependencies

    doc = fitz.open()
    page = doc.new_page(width=200, height=120)
    for i, h in enumerate((30, 60, 90)):
        page.draw_rect(
            fitz.Rect(30 + i * 50, 110 - h, 60 + i * 50, 110), color=(0, 0, 1), fill=(0, 0, 1)
        )
    page.insert_text((30, 15), "Sales 2024", fontsize=11)
    return page.get_pixmap().tobytes("png")


def test_vision_describes_a_chart_or_reports_why_not() -> None:
    describer = OpenAIImageDescriber(api_key=KEY, model=VISION, timeout_seconds=60, max_retries=1)
    try:
        result = run(describer.describe(_png(), media_type="image/png", context="Quarterly report"))
    except (TransientProcessingError, PermanentProcessingError) as exc:
        print("vision with", VISION, "->", exc.code, exc)
        pytest.skip(f"vision not available with {VISION}: {exc.code}")
    print("vision:", result)
    assert result.text.strip()


def test_speech_round_trip_or_reports_why_not() -> None:
    speech = OpenAISpeechProvider(
        api_key=KEY, stt_model=STT, tts_model=TTS, default_voice="alloy", timeout_seconds=60
    )

    async def speak() -> bytes:
        return b"".join([c async for c in speech.synthesize("DocuMind test.")])

    try:
        audio = run(speak())
    except (TransientProcessingError, PermanentProcessingError) as exc:
        print("tts with", TTS, "->", exc.code, exc)
        pytest.skip(f"speech not available with {TTS}: {exc.code}")
    print("tts bytes:", len(audio))
    text = run(speech.transcribe(audio, media_type="audio/mpeg"))
    print("stt:", text)
    assert "documind" in text.text.lower().replace(" ", "")


def test_configured_model_check_against_the_real_api() -> None:
    """F10: the start-up / `documind-manage ai check` result for the key in use."""
    from pydantic import SecretStr

    from documind.ops.ai_models import check_models

    settings = Settings().model_copy(
        update={
            "embedding_provider": "openai",
            "llm_provider": "openai",
            "llm_model": MISSING,
            "openai_api_key": SecretStr(KEY),
            "speech_provider": "none",
            "figure_descriptions_enabled": False,
        }
    )
    results = {r.purpose: r for r in run(check_models(settings))}
    print({k: (v.model, v.status) for k, v in results.items()})
    assert results["embeddings"].status == "available"
    assert results["answers"].status == "missing"
    assert results["answers"].detail == "not available to this API key"
