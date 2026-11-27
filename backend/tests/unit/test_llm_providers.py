import json
from typing import Any

import httpx2
import pytest

from documind.application.ports import ChatMessage, LLMDelta, LLMStreamEnd
from documind.domain.processing.errors import PermanentProcessingError, TransientProcessingError
from documind.domain.rag.prompts import INSUFFICIENT_CONTEXT_SENTINEL
from documind.infrastructure.llm.local_provider import LocalExtractiveLLM
from documind.infrastructure.llm.openai_provider import OpenAIChatProvider

MESSAGES = [ChatMessage("system", "rules"), ChatMessage("user", "question")]


def provider(handler: Any, *, retries: int = 1) -> OpenAIChatProvider:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return OpenAIChatProvider(
        api_key="sk-test", model="gpt-5.4-mini", max_retries=retries, http_client=client
    )


def completion(text: str) -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-5.4-mini",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128},
    }


def sse(chunks: list[dict[str, Any]]) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


def chunk(
    content: str | None = None, finish: str | None = None, usage: dict[str, int] | None = None
) -> dict[str, Any]:
    choices = (
        []
        if usage
        else [
            {"index": 0, "delta": {"content": content} if content else {}, "finish_reason": finish}
        ]
    )
    body: dict[str, Any] = {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gpt-5.4-mini",
        "choices": choices,
    }
    if usage:
        body["usage"] = usage
    return body


class TestOpenAIChat:
    async def test_generate(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(json.loads(request.content))
            return httpx2.Response(200, json=completion("Ninety days [1]."))

        result = await provider(handler).generate(MESSAGES, max_output_tokens=500, temperature=None)
        assert result.text == "Ninety days [1]."
        assert (result.usage.input_tokens, result.usage.output_tokens) == (120, 8)
        assert result.finish_reason == "stop"
        assert seen[0]["messages"] == [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "question"},
        ]
        assert seen[0]["max_completion_tokens"] == 500
        assert "temperature" not in seen[0]  # omitted unless configured

    async def test_temperature_sent_when_set(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(json.loads(request.content))
            return httpx2.Response(200, json=completion("x"))

        await provider(handler).generate(MESSAGES, max_output_tokens=10, temperature=0.1)
        assert seen[0]["temperature"] == 0.1

    async def test_stream_yields_deltas_then_usage(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(json.loads(request.content))
            body = sse(
                [
                    chunk("Ninety "),
                    chunk("days [1]."),
                    chunk(finish="stop"),
                    chunk(usage={"prompt_tokens": 50, "completion_tokens": 4, "total_tokens": 54}),
                ]
            )
            return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

        events = [
            e
            async for e in provider(handler).stream(
                MESSAGES, max_output_tokens=100, temperature=None
            )
        ]
        deltas = [e.text for e in events if isinstance(e, LLMDelta)]
        assert "".join(deltas) == "Ninety days [1]."
        end = events[-1]
        assert isinstance(end, LLMStreamEnd)
        assert (end.usage.input_tokens, end.usage.output_tokens, end.finish_reason) == (
            50,
            4,
            "stop",
        )
        assert seen[0]["stream"] is True
        assert seen[0]["stream_options"] == {"include_usage": True}

    @pytest.mark.parametrize(
        ("status", "error", "code"),
        [
            (429, TransientProcessingError, "llm_rate_limited"),
            (500, TransientProcessingError, "llm_unavailable"),
            (401, TransientProcessingError, "llm_misconfigured"),
            # The real API answers 404 model_not_found for a model the key can't use.
            (404, TransientProcessingError, "llm_misconfigured"),
            (400, PermanentProcessingError, "llm_rejected"),
        ],
    )
    async def test_errors_are_classified(
        self, status: int, error: type[Exception], code: str
    ) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                status, json={"error": {"message": "x"}}, headers={"retry-after-ms": "1"}
            )

        with pytest.raises(error) as exc:
            await provider(handler).generate(MESSAGES, max_output_tokens=10, temperature=None)
        assert exc.value.code == code  # type: ignore[attr-defined]
        with pytest.raises(error):
            async for _ in provider(handler).stream(
                MESSAGES, max_output_tokens=10, temperature=None
            ):
                pass


PROMPT = """<sources>
<source id="1" document="MSA" page="14">
The agreement renews automatically for one year terms.
Notice of non-renewal must be given ninety days before expiry.
</source>

<source id="2" document="Policy" page="3">
Renewals above fifty thousand require Legal review.
</source>
</sources>

<question>
How much notice is needed before renewal?
</question>"""


class TestLocalProvider:
    async def test_quotes_matching_sentences_with_citations(self) -> None:
        llm = LocalExtractiveLLM()
        result = await llm.generate(
            [ChatMessage("user", PROMPT)], max_output_tokens=100, temperature=None
        )
        assert "ninety days before expiry. [1]" in result.text
        assert result.usage.output_tokens > 0

    async def test_insufficient_when_nothing_matches(self) -> None:
        prompt = PROMPT.replace(
            "How much notice is needed before renewal?", "What is the parking policy?"
        )
        result = await LocalExtractiveLLM().generate(
            [ChatMessage("user", prompt)], max_output_tokens=100, temperature=None
        )
        assert result.text.startswith(INSUFFICIENT_CONTEXT_SENTINEL)

    async def test_stream_matches_generate(self) -> None:
        llm = LocalExtractiveLLM()
        generated = (
            await llm.generate(
                [ChatMessage("user", PROMPT)], max_output_tokens=100, temperature=None
            )
        ).text
        events = [
            e
            async for e in llm.stream(
                [ChatMessage("user", PROMPT)], max_output_tokens=100, temperature=None
            )
        ]
        assert "".join(e.text for e in events if isinstance(e, LLMDelta)) == generated
        assert isinstance(events[-1], LLMStreamEnd)
