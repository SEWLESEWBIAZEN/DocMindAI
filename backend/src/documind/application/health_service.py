"""Aggregates dependency health checks into a single report."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence

from documind.domain.health import (
    ComponentHealth,
    ComponentStatus,
    DependencyCheckError,
    HealthCheck,
    HealthReport,
)

logger = logging.getLogger(__name__)


class HealthService:
    """Runs all registered checks concurrently, each bounded by a timeout.

    A failing or hanging dependency never raises out of this service; it is
    reported as an ``error`` component so the caller can decide how to respond.
    """

    def __init__(self, checks: Sequence[HealthCheck], timeout_seconds: float) -> None:
        names = [c.name for c in checks]
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate health check names: {names}")
        self._checks = tuple(checks)
        self._timeout = timeout_seconds

    async def check_readiness(self) -> HealthReport:
        results = await asyncio.gather(*(self._run(check) for check in self._checks))
        return HealthReport(components=tuple(results))

    async def _run(self, check: HealthCheck) -> ComponentHealth:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                details = await check.check()
        except TimeoutError:
            error = f"timed out after {self._timeout:g}s"
        except DependencyCheckError as exc:
            error = str(exc)
        except Exception as exc:
            # The raw exception may contain hostnames or credentials; log it,
            # but only surface the exception type.
            error = type(exc).__name__
            logger.warning("Health check failed", extra={"component": check.name}, exc_info=True)
        else:
            return ComponentHealth(
                name=check.name,
                status=ComponentStatus.OK,
                latency_ms=_elapsed_ms(started),
                details=dict(details),
            )

        logger.warning("Dependency unhealthy", extra={"component": check.name, "error": error})
        return ComponentHealth(
            name=check.name,
            status=ComponentStatus.ERROR,
            latency_ms=_elapsed_ms(started),
            error=error,
        )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
