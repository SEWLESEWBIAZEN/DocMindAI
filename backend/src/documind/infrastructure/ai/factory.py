"""Builds AI adapters from a workspace's key and validates keys (ADR 0019).

Adding a provider means adding a builder and a validator here; nothing else
changes. Provider error messages are never passed through, because some
(e.g. "Incorrect API key provided: sk-...") echo parts of the key.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

import httpx2
import openai

from documind.application.ai.ports import ValidationResult
from documind.application.ai.resolver import CapabilityAdapter
from documind.core.config import Settings
from documind.domain.ai.providers import PROVIDERS
from documind.domain.billing.plans import Capability
from documind.infrastructure.embeddings.openai_provider import OpenAIEmbeddingProvider
from documind.infrastructure.llm.openai_provider import OpenAIChatProvider
from documind.infrastructure.llm.vision import OpenAIImageDescriber
from documind.infrastructure.speech.openai_provider import OpenAISpeechProvider

logger = logging.getLogger(__name__)


class ProviderAdapterFactory:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client_factory: Callable[[], httpx2.AsyncClient | None] = lambda: None,
    ) -> None:
        self._settings = settings
        self._http = http_client_factory

    # ------------------------------------------------------------------ models

    def models(self, provider: str, options: Mapping[str, str]) -> dict[Capability, str]:
        s = self._settings
        if provider != "openai":
            raise ValueError(f"provider {provider!r} is not available")
        return {
            Capability.EMBEDDING: options.get("embedding_model") or s.embedding_model,
            Capability.CHAT: options.get("chat_model") or s.llm_model,
            Capability.VISION: options.get("vision_model") or s.vision_model or s.llm_model,
            Capability.SPEECH_TO_TEXT: options.get("stt_model") or s.stt_model,
            Capability.TEXT_TO_SPEECH: options.get("tts_model") or s.tts_model,
        }

    def embedding_model(self, provider: str, options: Mapping[str, str]) -> str:
        return self.models(provider, options)[Capability.EMBEDDING]

    # ------------------------------------------------------------------ build

    def build(
        self, provider: str, secret: str, options: Mapping[str, str]
    ) -> dict[Capability, CapabilityAdapter]:
        spec = PROVIDERS.get(provider)
        if spec is None or spec.status != "available":
            raise ValueError(f"provider {provider!r} is not available")
        s = self._settings
        models = self.models(provider, options)
        http = self._http()
        adapters: dict[Capability, CapabilityAdapter] = {
            Capability.EMBEDDING: CapabilityAdapter(
                provider,
                OpenAIEmbeddingProvider(
                    api_key=secret,
                    model=models[Capability.EMBEDDING],
                    dimensions=s.embedding_dimensions,
                    timeout_seconds=s.embedding_timeout_seconds,
                    max_retries=s.embedding_max_retries,
                    http_client=http,
                ),
                models[Capability.EMBEDDING],
            ),
            Capability.CHAT: CapabilityAdapter(
                provider,
                OpenAIChatProvider(
                    api_key=secret,
                    model=models[Capability.CHAT],
                    timeout_seconds=s.llm_timeout_seconds,
                    max_retries=s.llm_max_retries,
                    http_client=http,
                ),
                models[Capability.CHAT],
            ),
        }
        if s.figure_descriptions_enabled and s.figure_max_per_document > 0:
            adapters[Capability.VISION] = CapabilityAdapter(
                provider,
                OpenAIImageDescriber(
                    api_key=secret,
                    model=models[Capability.VISION],
                    timeout_seconds=s.llm_timeout_seconds,
                    http_client=http,
                ),
                models[Capability.VISION],
            )
        speech = OpenAISpeechProvider(
            api_key=secret,
            stt_model=models[Capability.SPEECH_TO_TEXT],
            tts_model=models[Capability.TEXT_TO_SPEECH],
            default_voice=s.tts_voice,
            timeout_seconds=s.speech_timeout_seconds,
            http_client=http,
        )
        adapters[Capability.SPEECH_TO_TEXT] = CapabilityAdapter(
            provider, speech, models[Capability.SPEECH_TO_TEXT]
        )
        adapters[Capability.TEXT_TO_SPEECH] = CapabilityAdapter(
            provider, speech, models[Capability.TEXT_TO_SPEECH]
        )
        return adapters

    # --------------------------------------------------------------- validate

    async def validate(
        self, provider: str, secret: str, options: Mapping[str, str]
    ) -> ValidationResult:
        """One cheap call per required model: the key works and can use them."""
        if provider != "openai":
            return ValidationResult(
                False, "provider_not_available", "This provider isn't available yet."
            )
        models = self.models(provider, options)
        required = (models[Capability.EMBEDDING], models[Capability.CHAT])
        client = openai.AsyncOpenAI(
            api_key=secret, timeout=15.0, max_retries=1, http_client=self._http()
        )
        try:
            for model in required:
                try:
                    await client.models.retrieve(model)
                except openai.AuthenticationError:
                    return ValidationResult(
                        False, "provider_key_rejected", "OpenAI rejected this key."
                    )
                except (openai.PermissionDeniedError, openai.NotFoundError):
                    return ValidationResult(
                        False,
                        "model_not_available",
                        f"This key can't use the model '{model}'.",
                    )
                except openai.RateLimitError as exc:
                    if getattr(exc, "code", None) == "insufficient_quota":
                        return ValidationResult(
                            False,
                            "provider_quota_exhausted",
                            "The OpenAI account for this key is out of quota.",
                        )
                    # Rate limited but the key itself is accepted.
                except (openai.APIConnectionError, openai.APITimeoutError):
                    return ValidationResult(
                        False, "provider_unreachable", "OpenAI couldn't be reached. Try again."
                    )
                except openai.OpenAIError:
                    logger.warning(
                        "unexpected provider validation error", extra={"provider": provider}
                    )
                    return ValidationResult(
                        False, "provider_validation_failed", "The key couldn't be verified."
                    )
        finally:
            await client.close()
        return ValidationResult(True, checked_models=required)
