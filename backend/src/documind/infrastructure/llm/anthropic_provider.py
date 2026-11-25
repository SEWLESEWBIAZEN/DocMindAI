"""Claude (Anthropic Messages API) behind the LLMProvider port (generate + stream).

Works with Anthropic directly or with a compatible gateway (``ANTHROPIC_BASE_URL``).

Notes for current Claude models (Claude Opus 5.5 is the default):

* Thinking is always on and its tokens count towards ``max_tokens``, so every
  request gets ``ANTHROPIC_THINKING_HEADROOM_TOKENS`` on top of what the caller
  asked for. Depth is set with ``output_config.effort``, not a token budget.
* Sampling parameters (``temperature``) are not accepted and are never sent.
* System messages become the top-level ``system`` prompt.
* A safety decline comes back as ``stop_reason == "refusal"``. With
  ``ANTHROPIC_REFUSAL_FALLBACK`` on (default), the API re-runs a declined
  request on a fallback model inside the same call (server-side fallbacks).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, NoReturn

import anthropic

from documind.application.ports import (
    ChatMessage,
    LLMDelta,
    LLMResult,
    LLMStreamEnd,
    LLMStreamEvent,
    LLMUsage,
)
from documind.domain.processing.errors import PermanentProcessingError, TransientProcessingError

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Claude's stop reasons in the port's vocabulary (the RAG layer reads "length").
_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}


def gateway_base_url(url: str | None) -> str | None:
    """The SDK appends ``/v1/messages`` itself: accept gateway URLs given with or without /v1."""
    if not url:
        return None
    url = url.rstrip("/")
    return url[: -len("/v1")] if url.endswith("/v1") else url


def raise_classified(exc: Exception) -> NoReturn:
    if isinstance(exc, anthropic.RateLimitError):
        raise TransientProcessingError(
            "The AI service is busy. Please try again shortly.", code="llm_rate_limited"
        ) from exc
    if isinstance(
        exc,
        anthropic.APIConnectionError
        | anthropic.APITimeoutError
        | anthropic.InternalServerError
        | anthropic.OverloadedError,
    ):
        raise TransientProcessingError(
            "The AI service is temporarily unavailable.", code="llm_unavailable"
        ) from exc
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        logger.error("LLM provider rejected our credentials")
        raise TransientProcessingError(
            "The AI service is misconfigured.", code="llm_misconfigured"
        ) from exc
    if isinstance(exc, anthropic.NotFoundError):
        logger.error("LLM model or endpoint not found (check LLM_MODEL / ANTHROPIC_BASE_URL)")
        raise TransientProcessingError(
            "The AI service is misconfigured.", code="llm_misconfigured"
        ) from exc
    if isinstance(exc, anthropic.BadRequestError):
        raise PermanentProcessingError(
            "The AI service rejected this request.", code="llm_rejected"
        ) from exc
    if isinstance(exc, anthropic.APIStatusError):
        raise TransientProcessingError(
            f"Unexpected AI service response ({exc.status_code}).", code="llm_error"
        ) from exc
    raise exc


def _usage(raw: Any) -> LLMUsage:
    # Cached and uncached input are both input; the ledger records the total.
    input_tokens = (
        int(getattr(raw, "input_tokens", 0) or 0)
        + int(getattr(raw, "cache_read_input_tokens", 0) or 0)
        + int(getattr(raw, "cache_creation_input_tokens", 0) or 0)
    )
    return LLMUsage(
        input_tokens=input_tokens, output_tokens=int(getattr(raw, "output_tokens", 0) or 0)
    )


def _refused() -> PermanentProcessingError:
    return PermanentProcessingError(
        "The AI model declined to answer this request.", code="llm_refused"
    )


class AnthropicChatProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        effort: str = "medium",
        thinking_headroom_tokens: int = 4096,
        refusal_fallback: bool = True,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        http_client: Any | None = None,
    ) -> None:
        self._model = model
        self._effort = effort
        self._headroom = thinking_headroom_tokens
        self._fallback = refusal_fallback
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=gateway_base_url(base_url),
            timeout=timeout_seconds,
            max_retries=max_retries,
            http_client=http_client,
        )

    @property
    def model(self) -> str:
        return self._model

    def _params(self, messages: list[ChatMessage], max_output_tokens: int) -> dict[str, Any]:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        turns = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role in ("user", "assistant")
        ]
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_output_tokens + self._headroom,
            "messages": turns,
            "output_config": {"effort": self._effort},
        }
        if system:
            params["system"] = system
        if self._fallback:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    async def generate(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> LLMResult:
        del temperature  # not accepted by current Claude models
        try:
            response: Any = await self._client.beta.messages.create(
                **self._params(messages, max_output_tokens)
            )
        except anthropic.AnthropicError as exc:
            raise_classified(exc)
        text = "".join(b.text for b in response.content if b.type == "text")
        if response.stop_reason == "refusal" and not text:
            raise _refused()
        return LLMResult(
            text=text,
            usage=_usage(response.usage),
            model=response.model or self._model,
            finish_reason=_FINISH.get(response.stop_reason, response.stop_reason),
        )

    async def stream(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> AsyncIterator[LLMStreamEvent]:
        del temperature
        sent_text = False
        try:
            async with self._client.beta.messages.stream(
                **self._params(messages, max_output_tokens)
            ) as stream:
                async for text in stream.text_stream:
                    if text:
                        sent_text = True
                        yield LLMDelta(text)
                final: Any = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise_classified(exc)
        if final.stop_reason == "refusal" and not sent_text:
            raise _refused()
        yield LLMStreamEnd(
            usage=_usage(final.usage),
            model=final.model or self._model,
            finish_reason=_FINISH.get(final.stop_reason, final.stop_reason),
        )
