"""Plan catalog and entitlements (ADR 0020).

This module is the source of truth for plans. Migrations seed the ``plans``
table from a copy of it, and a test asserts the two never drift.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any

GIB = 1024**3


class Capability(StrEnum):
    """Metered AI capabilities. One usage record per provider call."""

    EMBEDDING = "embedding"
    CHAT = "chat"
    VISION = "vision"
    SPEECH_TO_TEXT = "speech_to_text"
    TEXT_TO_SPEECH = "text_to_speech"


class BilledTo(StrEnum):
    PLATFORM = "platform"
    WORKSPACE_KEY = "workspace_key"


class PlanKey(StrEnum):
    FREE = "free"
    PRO = "pro"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"


@dataclass(frozen=True, slots=True)
class Entitlements:
    """What a workspace may do. ``None`` means unlimited."""

    # Limits (monthly where noted).
    max_members: int | None
    max_documents: int | None
    storage_bytes: int | None
    chat_tokens: int | None  # monthly, platform-billed input + output
    embedding_tokens: int | None  # monthly, platform-billed
    audio_seconds: int | None  # monthly, platform-billed STT + TTS
    figure_descriptions: int | None  # monthly, platform-billed
    # Features.
    byok: bool
    platform_ai: bool
    server_voice: bool
    custom_storage: bool
    dedicated_database: bool
    external_database: bool
    customer_workers: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_overrides(self, overrides: dict[str, Any] | None) -> Entitlements:
        """Apply per-workspace overrides, ignoring unknown keys."""
        if not overrides:
            return self
        names = {f.name for f in fields(self)}
        return replace(self, **{k: v for k, v in overrides.items() if k in names})


class BillingInterval(StrEnum):
    MONTH = "month"
    YEAR = "year"


#: Annual billing charges ten months: two months free.
ANNUAL_MONTHS_CHARGED = 10


@dataclass(frozen=True, slots=True)
class Pricing:
    """List prices in US cents, per member (seat). ``None``: by quote (contact sales)."""

    monthly_cents_per_member: int | None
    self_serve: bool

    @property
    def annual_cents_per_member(self) -> int | None:
        if self.monthly_cents_per_member is None:
            return None
        return self.monthly_cents_per_member * ANNUAL_MONTHS_CHARGED

    def cents_per_member(self, interval: BillingInterval) -> int | None:
        if interval is BillingInterval.YEAR:
            return self.annual_cents_per_member
        return self.monthly_cents_per_member


@dataclass(frozen=True, slots=True)
class Plan:
    key: PlanKey
    name: str
    description: str
    entitlements: Entitlements
    pricing: Pricing = Pricing(0, self_serve=False)

    @property
    def paid(self) -> bool:
        return self.key is not PlanKey.FREE


PLANS: MappingProxyType[PlanKey, Plan] = MappingProxyType(
    {
        PlanKey.FREE: Plan(
            PlanKey.FREE,
            "Free",
            "For individuals and small teams getting started.",
            Entitlements(
                max_members=5,
                max_documents=200,
                storage_bytes=1 * GIB,
                chat_tokens=2_000_000,
                embedding_tokens=20_000_000,
                audio_seconds=3_600,
                figure_descriptions=200,
                byok=True,  # ADR 0019 decision 3: every plan, including Free
                platform_ai=True,
                server_voice=True,
                custom_storage=False,
                dedicated_database=False,
                external_database=False,
                customer_workers=False,
            ),
            Pricing(0, self_serve=False),
        ),
        PlanKey.PRO: Plan(
            PlanKey.PRO,
            "Pro",
            "For growing teams with larger libraries.",
            Entitlements(
                max_members=25,
                max_documents=5_000,
                storage_bytes=50 * GIB,
                chat_tokens=20_000_000,
                embedding_tokens=200_000_000,
                audio_seconds=36_000,
                figure_descriptions=5_000,
                byok=True,
                platform_ai=True,
                server_voice=True,
                custom_storage=True,
                dedicated_database=False,
                external_database=False,
                customer_workers=False,
            ),
            Pricing(1_900, self_serve=True),
        ),
        PlanKey.BUSINESS: Plan(
            PlanKey.BUSINESS,
            "Business",
            "For organizations that need their own storage and databases.",
            Entitlements(
                max_members=250,
                max_documents=50_000,
                storage_bytes=500 * GIB,
                chat_tokens=200_000_000,
                embedding_tokens=2_000_000_000,
                audio_seconds=360_000,
                figure_descriptions=50_000,
                byok=True,
                platform_ai=True,
                server_voice=True,
                custom_storage=True,
                dedicated_database=True,
                external_database=True,
                customer_workers=False,
            ),
            Pricing(9_900, self_serve=True),
        ),
        PlanKey.ENTERPRISE: Plan(
            PlanKey.ENTERPRISE,
            "Enterprise",
            "Custom limits, customer-managed infrastructure and processing.",
            Entitlements(
                max_members=None,
                max_documents=None,
                storage_bytes=None,
                chat_tokens=None,
                embedding_tokens=None,
                audio_seconds=None,
                figure_descriptions=None,
                byok=True,
                platform_ai=True,
                server_voice=True,
                custom_storage=True,
                dedicated_database=True,
                external_database=True,
                customer_workers=True,
            ),
            Pricing(None, self_serve=False),
        ),
    }
)


#: Infrastructure features, the entitlement each needs, and how to describe it.
INFRASTRUCTURE_FEATURES: MappingProxyType[str, str] = MappingProxyType(
    {
        "custom_storage": "your own object storage",
        "dedicated_database": "a dedicated database",
        "external_database": "your own (external) database",
        "customer_workers": "processing on your own servers",
    }
)


def cheapest_plan_with(feature: str) -> PlanKey | None:
    """The lowest plan that includes an infrastructure feature."""
    for key in (PlanKey.FREE, PlanKey.PRO, PlanKey.BUSINESS, PlanKey.ENTERPRISE):
        if getattr(PLANS[key].entitlements, feature):
            return key
    return None


DEFAULT_PLAN = PlanKey.FREE

#: Text-to-speech is metered in characters; this converts to audio seconds.
TTS_CHARS_PER_SECOND = 15
