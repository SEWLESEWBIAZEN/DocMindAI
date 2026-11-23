import asyncio
import time
from collections.abc import Mapping

import pytest

from documind.application.health_service import HealthService
from documind.domain.health import (
    ComponentStatus,
    DependencyCheckError,
    DetailValue,
    HealthCheck,
    OverallStatus,
)


class FakeCheck:
    def __init__(
        self,
        name: str,
        *,
        details: Mapping[str, DetailValue] | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.name = name
        self._details = details or {}
        self._error = error
        self._delay = delay

    async def check(self) -> Mapping[str, DetailValue]:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._details


def test_fake_satisfies_port() -> None:
    assert isinstance(FakeCheck("x"), HealthCheck)


async def test_all_healthy() -> None:
    service = HealthService([FakeCheck("a", details={"v": "1"}), FakeCheck("b")], timeout_seconds=1)
    report = await service.check_readiness()
    assert report.status is OverallStatus.OK
    assert report.is_healthy
    assert [c.name for c in report.components] == ["a", "b"]
    assert report.components[0].details == {"v": "1"}
    assert all(c.latency_ms >= 0 for c in report.components)


async def test_dependency_error_message_is_reported() -> None:
    service = HealthService(
        [FakeCheck("ok"), FakeCheck("db", error=DependencyCheckError("pgvector missing"))],
        timeout_seconds=1,
    )
    report = await service.check_readiness()
    assert report.status is OverallStatus.DEGRADED
    failed = report.components[1]
    assert failed.status is ComponentStatus.ERROR
    assert failed.error == "pgvector missing"
    assert failed.details == {}


async def test_unexpected_error_does_not_leak_message() -> None:
    secret = "postgresql://admin:hunter2@db"
    service = HealthService([FakeCheck("db", error=ConnectionError(secret))], timeout_seconds=1)
    report = await service.check_readiness()
    assert report.components[0].error == "ConnectionError"
    assert secret not in str(report)


async def test_hanging_check_times_out() -> None:
    service = HealthService([FakeCheck("slow", delay=5)], timeout_seconds=0.05)
    report = await service.check_readiness()
    assert report.components[0].status is ComponentStatus.ERROR
    assert report.components[0].error == "timed out after 0.05s"


async def test_checks_run_concurrently() -> None:
    service = HealthService([FakeCheck(str(i), delay=0.2) for i in range(5)], timeout_seconds=1)
    started = time.perf_counter()
    await service.check_readiness()
    assert time.perf_counter() - started < 0.6


def test_duplicate_names_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        HealthService([FakeCheck("a"), FakeCheck("a")], timeout_seconds=1)
