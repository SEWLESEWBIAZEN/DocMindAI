"""Health-check domain model and port.

Infrastructure adapters implement :class:`HealthCheck`; the application layer
aggregates them into a :class:`HealthReport`. Nothing here depends on FastAPI,
SQLAlchemy, Redis or boto3.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

DetailValue = str | int | float | bool | None


class ComponentStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class OverallStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"


class DependencyCheckError(Exception):
    """Raised by a health check with a message that is safe to show to API clients."""


@runtime_checkable
class HealthCheck(Protocol):
    """Port implemented by each dependency probe."""

    @property
    def name(self) -> str: ...

    async def check(self) -> Mapping[str, DetailValue]:
        """Probe the dependency. Return diagnostic details, or raise on failure."""
        ...


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    name: str
    status: ComponentStatus
    latency_ms: float
    details: Mapping[str, DetailValue] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    components: tuple[ComponentHealth, ...]

    @property
    def status(self) -> OverallStatus:
        if all(c.status is ComponentStatus.OK for c in self.components):
            return OverallStatus.OK
        return OverallStatus.DEGRADED

    @property
    def is_healthy(self) -> bool:
        return self.status is OverallStatus.OK
