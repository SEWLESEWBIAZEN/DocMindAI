"""Check that the configured AI models are available to the configured keys (F10).

An API key can be limited to some models (OpenAI project keys often are). If
``LLM_MODEL`` names one the key can't use, every answer fails, and nothing says so
until a user asks a question. This asks each provider about each configured model:

* OpenAI ``GET /v1/models/{model}`` answers 404 for a model the key can't use (checked
  against a restricted project key during verification) and costs nothing;
* Anthropic ``GET /v1/models/{model}`` likewise.

Used by ``documind-manage ai check``, before ``documind-manage eval``, and once in the
background when the API starts (logged, and exported as ``documind_ai_model_available``).
Workspace keys (BYOK) are checked when they are saved, not here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Any

import anthropic
import openai

from documind.core.config import Settings
from documind.infrastructure.llm.anthropic_provider import gateway_base_url

logger = logging.getLogger(__name__)

AVAILABLE, MISSING, ERROR = "available", "missing", "error"


@dataclass(frozen=True)
class ModelCheck:
    purpose: str  # answers, embeddings, vision, speech-to-text, text-to-speech
    provider: str
    model: str
    status: str  # available, missing (the key can't use it), error (couldn't tell)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def configured_models(settings: Settings) -> list[tuple[str, str, str]]:
    """(purpose, provider, model) for every model a platform key is used with."""
    wanted: list[tuple[str, str, str]] = []
    if settings.llm_provider in ("openai", "anthropic"):
        wanted.append(("answers", settings.llm_provider, settings.llm_model))
    if settings.embedding_provider == "openai":
        wanted.append(("embeddings", "openai", settings.embedding_model))
    if (
        settings.figure_descriptions_enabled
        and settings.figure_max_per_document > 0
        and settings.llm_provider != "local"
    ):
        # Figure descriptions always use OpenAI (see container._build_image_describer).
        wanted.append(("vision", "openai", settings.vision_model or settings.llm_model))
    if settings.speech_provider == "openai":
        wanted.append(("speech-to-text", "openai", settings.stt_model))
        wanted.append(("text-to-speech", "openai", settings.tts_model))
    return wanted


def _status_of(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, openai.NotFoundError | anthropic.NotFoundError):
        return MISSING, "not available to this API key"
    if isinstance(exc, openai.AuthenticationError | anthropic.AuthenticationError):
        return ERROR, "the API key was rejected"
    if isinstance(exc, openai.PermissionDeniedError | anthropic.PermissionDeniedError):
        return MISSING, "this API key is not allowed to use it"
    if isinstance(exc, openai.APIStatusError | anthropic.APIStatusError):
        return ERROR, f"provider answered HTTP {exc.status_code}"
    if isinstance(exc, openai.APIConnectionError | anthropic.APIConnectionError):
        return ERROR, "could not reach the provider"
    return ERROR, type(exc).__name__


async def check_models(settings: Settings, *, timeout_seconds: float = 15.0) -> list[ModelCheck]:
    wanted = configured_models(settings)
    if not wanted:
        return []
    clients: dict[str, Any] = {}
    if any(p == "openai" for _, p, _ in wanted) and settings.openai_api_key is not None:
        clients["openai"] = openai.AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            base_url=settings.openai_base_url,
            timeout=timeout_seconds,
            max_retries=1,
        )
    if any(p == "anthropic" for _, p, _ in wanted) and settings.anthropic_api_key is not None:
        clients["anthropic"] = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            base_url=gateway_base_url(settings.anthropic_base_url),
            timeout=timeout_seconds,
            max_retries=1,
        )

    async def one(purpose: str, provider: str, model: str) -> ModelCheck:
        client = clients.get(provider)
        if client is None:
            return ModelCheck(purpose, provider, model, ERROR, "no API key configured")
        try:
            await client.models.retrieve(model)
        except Exception as exc:
            status, detail = _status_of(exc)
            return ModelCheck(purpose, provider, model, status, detail)
        return ModelCheck(purpose, provider, model, AVAILABLE)

    try:
        return list(await asyncio.gather(*(one(*w) for w in wanted)))
    finally:
        for client in clients.values():
            await client.close()


def log_results(results: list[ModelCheck]) -> None:
    """One line per problem; never logs keys."""
    for r in results:
        if r.status == MISSING:
            logger.error(
                "Configured AI model is not available: requests that need it will fail",
                extra={"purpose": r.purpose, "provider": r.provider, "model": r.model},
            )
        elif r.status == ERROR:
            logger.warning(
                "Could not check a configured AI model",
                extra={"purpose": r.purpose, "model": r.model, "reason": r.detail},
            )
