"""Port for building and validating AI adapters from a workspace key (ADR 0019)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from documind.application.ai.resolver import CapabilityAdapter
from documind.domain.billing.plans import Capability


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    code: str | None = None
    message: str | None = None  # user-safe; never the provider's own text
    checked_models: tuple[str, ...] = field(default_factory=tuple)


class ProviderFactory(Protocol):
    def models(self, provider: str, options: Mapping[str, str]) -> dict[Capability, str]: ...

    def embedding_model(self, provider: str, options: Mapping[str, str]) -> str: ...

    def build(
        self, provider: str, secret: str, options: Mapping[str, str]
    ) -> dict[Capability, CapabilityAdapter]: ...

    async def validate(
        self, provider: str, secret: str, options: Mapping[str, str]
    ) -> ValidationResult: ...
