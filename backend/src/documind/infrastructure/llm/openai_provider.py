"""OpenAI chat completions behind the LLMProvider port (generate + stream)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, NoReturn

import httpx2
import openai

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


def raise_classified(exc: Exception) -> NoReturn:
    if isinstance(exc, openai.RateLimitError):
        raise TransientProcessingError(
            "The AI service is busy. Please try again shortly.", code="llm_rate_limited"
        ) from exc
    if isinstance(
        exc, openai.APIConnectionError | openai.APITimeoutError | openai.InternalServerError
    ):
        raise TransientProcessingError(
            "The AI service is temporarily unavailable.", code="llm_unavailable"
        ) from exc
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        logger.error("LLM provider rejected our credentials")
        raise TransientProcessingError(
            "The AI service is misconfigured.", code="llm_misconfigured"
        ) from exc
    if isinstance(exc, openai.BadRequestError):
        raise PermanentProcessingError(
            "The AI service rejected this request.", code="llm_rejected"
        ) from exc
    if isinstance(exc, openai.APIStatusError):
        raise TransientProcessingError(
            f"Unexpected AI service response ({exc.status_code}).", code="llm_error"
        ) from exc
    raise exc


def _usage(raw: Any) -> LLMUsage:
    return LLMUsage(
        input_tokens=int(getattr(raw, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(raw, "completion_tokens", 0) or 0),
    )


class OpenAIChatProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=max_retries,
            http_client=http_client,
        )

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def _messages(messages: list[ChatMessage]) -> list[dict[str, str]]:
        return [{"role": m.role, "content": m.content} for m in messages]

    def _params(
        self, messages: list[ChatMessage], max_output_tokens: int, temperature: float | None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self._model,
            "messages": self._messages(messages),
            "max_completion_tokens": max_output_tokens,
        }
        # Reasoning models only accept the default temperature; send it only when set.
        if temperature is not None:
            params["temperature"] = temperature
        return params

    async def generate(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> LLMResult:
        try:
            response: Any = await self._client.chat.completions.create(
                **self._params(messages, max_output_tokens, temperature)
            )
        except openai.OpenAIError as exc:
            raise_classified(exc)
        choice = response.choices[0]
        return LLMResult(
            text=choice.message.content or "",
            usage=_usage(response.usage),
            model=response.model or self._model,
            finish_reason=choice.finish_reason,
        )

    async def stream(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> AsyncIterator[LLMStreamEvent]:
        try:
            stream: Any = await self._client.chat.completions.create(
                **self._params(messages, max_output_tokens, temperature),
                stream=True,
                stream_options={"include_usage": True},
            )
            usage = LLMUsage()
            finish: str | None = None
            model = self._model
            async for chunk in stream:
                model = chunk.model or model
                if chunk.usage is not None:  # final chunk when include_usage is set
                    usage = _usage(chunk.usage)
                for choice in chunk.choices:
                    if choice.delta and choice.delta.content:
                        yield LLMDelta(choice.delta.content)
                    if choice.finish_reason:
                        finish = choice.finish_reason
        except openai.OpenAIError as exc:
            raise_classified(exc)
        yield LLMStreamEnd(usage=usage, model=model, finish_reason=finish)
