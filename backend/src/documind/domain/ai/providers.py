"""AI provider catalog for workspace keys (ADR 0019).

Provider-neutral: a key record stores ``provider`` plus non-secret settings.
Only providers marked ``available`` can be configured; ``planned`` entries are
listed so the UI and API can say what's coming, but are refused until an
adapter is implemented and tested (no checklist-only providers).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from documind.domain.billing.plans import Capability

ALL = frozenset(Capability)
_MODEL = re.compile(r"^[A-Za-z0-9._:/-]{1,100}$")


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    key: str
    name: str
    capabilities: frozenset[Capability]
    status: Literal["available", "planned"]
    #: Non-secret settings this provider accepts (all optional model overrides).
    settings: tuple[str, ...] = ()


PROVIDERS: MappingProxyType[str, ProviderSpec] = MappingProxyType(
    {
        "openai": ProviderSpec(
            "openai",
            "OpenAI",
            ALL,
            "available",
            ("embedding_model", "chat_model", "vision_model", "stt_model", "tts_model"),
        ),
        "azure_openai": ProviderSpec("azure_openai", "Azure OpenAI", ALL, "planned"),
        "anthropic": ProviderSpec(
            "anthropic", "Anthropic", frozenset({Capability.CHAT, Capability.VISION}), "planned"
        ),
        "google": ProviderSpec("google", "Google", ALL, "planned"),
        "mistral": ProviderSpec(
            "mistral", "Mistral", frozenset({Capability.CHAT, Capability.EMBEDDING}), "planned"
        ),
        "openai_compatible": ProviderSpec(
            "openai_compatible",
            "OpenAI-compatible or self-hosted",
            frozenset({Capability.CHAT, Capability.EMBEDDING}),
            "planned",
        ),
    }
)


def clean_settings(spec: ProviderSpec, raw: dict[str, object] | None) -> dict[str, str]:
    """Validate provider settings; raises ValueError with a user-safe message."""
    out: dict[str, str] = {}
    for name, value in (raw or {}).items():
        if name not in spec.settings:
            raise ValueError(f"'{name}' isn't a setting for {spec.name}.")
        if value in (None, ""):
            continue
        if not isinstance(value, str) or not _MODEL.match(value):
            raise ValueError(f"'{name}' must be a model name.")
        out[name] = value
    return out
