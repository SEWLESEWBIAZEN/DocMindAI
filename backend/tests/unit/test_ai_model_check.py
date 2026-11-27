"""Configured-model availability check (production readiness, F10)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest

from documind.ops import ai_models
from documind.ops.ai_models import ModelCheck, check_models, configured_models
from documind.ops.metrics import _Out, ai_model_gauges
from tests.conftest import SettingsFactory


def _error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    request = httpx2.Request("GET", "https://api.openai.com/v1/models/m")
    return cls("x", response=httpx2.Response(status, request=request), body=None)


def test_only_hosted_providers_are_checked(make_settings: SettingsFactory) -> None:
    local = make_settings(embedding_provider="local", llm_provider="local")
    assert configured_models(local) == []
    hosted = make_settings(
        embedding_provider="openai",
        llm_provider="openai",
        llm_model="chat-1",
        openai_api_key="sk-test",
        speech_provider="openai",
        figure_descriptions_enabled=True,
    )
    assert configured_models(hosted) == [
        ("answers", "openai", "chat-1"),
        ("embeddings", "openai", "text-embedding-3-small"),
        ("vision", "openai", "chat-1"),  # VISION_MODEL unset: the answer model
        ("speech-to-text", "openai", "gpt-4o-mini-transcribe"),
        ("text-to-speech", "openai", "gpt-4o-mini-tts"),
    ]


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (_error(openai.NotFoundError, 404), "missing"),
        (_error(openai.PermissionDeniedError, 403), "missing"),
        (_error(openai.AuthenticationError, 401), "error"),
        (_error(openai.InternalServerError, 500), "error"),
        (openai.APIConnectionError(request=httpx2.Request("GET", "https://x")), "error"),
    ],
)
def test_failures_are_classified(
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    status: str,
) -> None:
    settings = make_settings(
        embedding_provider="openai", llm_provider="openai", openai_api_key="sk-test"
    )

    class Models:
        async def retrieve(self, model: str) -> Any:
            if model == settings.llm_model:
                raise exc
            return SimpleNamespace(id=model)

    class Client:
        models = Models()

        def __init__(self, **_: Any) -> None: ...

        async def close(self) -> None: ...

    monkeypatch.setattr(ai_models.openai, "AsyncOpenAI", Client)
    results = {r.purpose: r for r in asyncio.run(check_models(settings))}
    assert results["answers"].status == status
    assert results["embeddings"].status == "available"
    assert "sk-test" not in repr(results)


def test_metric_reports_available_and_missing_models() -> None:
    out = _Out()
    ai_model_gauges(
        out,
        [
            ModelCheck("answers", "openai", "chat-1", "missing", "not available to this API key"),
            ModelCheck("embeddings", "openai", "emb-1", "available"),
            ModelCheck("vision", "openai", "v-1", "error", "could not reach the provider"),
        ],
    )
    text = out.text()
    assert 'documind_ai_model_available{model="chat-1",purpose="answers"} 0.0' in text
    assert 'documind_ai_model_available{model="emb-1",purpose="embeddings"} 1.0' in text
    assert "v-1" not in text  # unknown is not reported as missing
