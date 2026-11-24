"""Background sweeps across every data plane (ADR 0021)."""

from __future__ import annotations

import dataclasses
import logging

from documind.application.conversations.sweeper import StaleReplySweeper
from documind.application.infrastructure.resolver import InfrastructureResolver
from documind.application.ports import JobDispatcher
from documind.application.processing.sweeper import ProcessingSweeper, SweepResult
from documind.core.config import Settings
from documind.infrastructure.database.dataplane_runtime import DataPlane, relay_outbox

logger = logging.getLogger(__name__)


async def _planes(resolver: InfrastructureResolver) -> list[DataPlane]:
    return [resolver.shared, *(plane for plane, _ in await resolver.isolated_planes())]


async def sweep_processing(
    resolver: InfrastructureResolver, settings: Settings, dispatcher: JobDispatcher
) -> SweepResult:
    total = SweepResult(0, 0, 0, 0)
    for plane in await _planes(resolver):
        try:
            result = await ProcessingSweeper(plane.sessions, settings, dispatcher).sweep()
            if plane.isolated:
                await relay_outbox(plane.sessions, resolver.shared.sessions)
        except Exception:
            # One customer database being down must not stop the others.
            logger.exception("processing sweep failed for a data plane", extra={"plane": plane.key})
            continue
        total = SweepResult(
            *(
                a + b
                for a, b in zip(
                    dataclasses.astuple(total), dataclasses.astuple(result), strict=True
                )
            )
        )
    return total


async def sweep_replies(resolver: InfrastructureResolver, *, stale_after_seconds: int) -> int:
    failed = 0
    for plane in await _planes(resolver):
        try:
            failed += await StaleReplySweeper(
                plane.sessions, stale_after_seconds=stale_after_seconds
            ).sweep()
        except Exception:
            logger.exception("reply sweep failed for a data plane", extra={"plane": plane.key})
    return failed
