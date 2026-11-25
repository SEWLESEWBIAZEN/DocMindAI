"""Claude adapter (LLM_PROVIDER=anthropic) against a mocked Messages API."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from documind.application.ports import ChatMessage, LLMDelta, LLMStreamEnd
from documind.domain.processing.errors import PermanentProcessingError, TransientProcessingError
from documind.infrastructure.llm.anthropic_provider import FALLBACK_BETA, AnthropicChatProvider

MESSAGES = [
    ChatMessage("system", "Answer from the sources only."),
    ChatMessage("user", "How much notice?"),
    ChatMessage("assistant", "Thirty days."),
    ChatMessage("user", "And for contractors?"),
]


def _message(text: str = "Fourteen days [1].", stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "s"}]
        + ([{"type": "text", "text": text}] if text else []),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50},
    }


def provider(handler: Any, **kwargs: Any) -> tuple[AnthropicChatProvider, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(record))
    return (
        AnthropicChatProvider(
            api_key="sk-test",
            model="claude-opus-5-5",
            base_url="https://gateway.example/v1",
            max_retries=0,
            http_client=client,
            **kwargs,
        ),
        seen,
    )


async def test_generate_maps_the_conversation() -> None:
    llm, seen = provider(lambda r: httpx2.Response(200, json=_message()))
    result = await llm.generate(MESSAGES, max_output_tokens=800, temperature=0.2)
    assert result.text == "Fourteen days [1]."
    assert result.finish_reason == "stop"
    assert result.usage.input_tokens == 150  # cached input counts too
    assert result.usage.output_tokens == 20
    body = json.loads(seen[0].content)
    # A gateway URL given with /v1 isn't doubled.
    assert str(seen[0].url) == "https://gateway.example/v1/messages"
    # SDK 2.x sends the key as a bearer token, with anthropic-version 2026-10-21.
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    assert seen[0].headers["anthropic-version"]
    assert body["system"] == "Answer from the sources only."
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["max_tokens"] == 800 + 4096  # thinking headroom
    assert body["output_config"] == {"effort": "medium"}
    assert "temperature" not in body  # not accepted by current Claude models
    assert "thinking" not in body  # always on; effort controls it
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in seen[0].headers["anthropic-beta"]


async def test_fallbacks_can_be_turned_off_for_gateways() -> None:
    llm, seen = provider(lambda r: httpx2.Response(200, json=_message()), refusal_fallback=False)
    await llm.generate(MESSAGES, max_output_tokens=100, temperature=None)
    body = json.loads(seen[0].content)
    assert "fallbacks" not in body
    assert "anthropic-beta" not in seen[0].headers


async def test_truncated_and_refused_answers() -> None:
    llm, _ = provider(lambda r: httpx2.Response(200, json=_message(stop_reason="max_tokens")))
    assert (await llm.generate(MESSAGES, max_output_tokens=10, temperature=None)).finish_reason == (
        "length"
    )
    llm, _ = provider(lambda r: httpx2.Response(200, json=_message("", "refusal")))
    with pytest.raises(PermanentProcessingError) as exc:
        await llm.generate(MESSAGES, max_output_tokens=10, temperature=None)
    assert exc.value.code == "llm_refused"


def _sse(*events: tuple[str, dict[str, Any]]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


async def test_stream_yields_text_then_usage() -> None:
    start = _message("")
    start["content"] = []
    start["stop_reason"] = None
    start["usage"] = {"input_tokens": 100, "output_tokens": 1}
    body = _sse(
        ("message_start", {"type": "message_start", "message": start}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Fourteen "},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "days."},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 30},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )
    llm, seen = provider(
        lambda r: httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})
    )
    events = [e async for e in llm.stream(MESSAGES, max_output_tokens=500, temperature=None)]
    assert [e.text for e in events if isinstance(e, LLMDelta)] == ["Fourteen ", "days."]
    end = events[-1]
    assert isinstance(end, LLMStreamEnd)
    assert end.finish_reason == "stop"
    assert end.usage.output_tokens == 30
    assert json.loads(seen[0].content)["stream"] is True


@pytest.mark.parametrize(
    ("status", "code", "kind"),
    [
        (429, "llm_rate_limited", TransientProcessingError),
        (529, "llm_unavailable", TransientProcessingError),
        (401, "llm_misconfigured", TransientProcessingError),
        (404, "llm_misconfigured", TransientProcessingError),
        (400, "llm_rejected", PermanentProcessingError),
    ],
)
async def test_errors_are_classified(status: int, code: str, kind: type[Exception]) -> None:
    llm, _ = provider(
        lambda r: httpx2.Response(
            status, json={"type": "error", "error": {"type": "x", "message": "no"}}
        )
    )
    with pytest.raises(kind) as exc:
        await llm.generate(MESSAGES, max_output_tokens=10, temperature=None)
    assert exc.value.code == code  # type: ignore[attr-defined]


def test_gateway_urls_with_or_without_v1() -> None:
    from documind.infrastructure.llm.anthropic_provider import gateway_base_url

    assert gateway_base_url("https://gw.example/v1") == "https://gw.example"
    assert gateway_base_url("https://gw.example/v1/") == "https://gw.example"
    assert gateway_base_url("https://gw.example") == "https://gw.example"
    assert gateway_base_url("") is None
