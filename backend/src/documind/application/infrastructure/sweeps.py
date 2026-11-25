"""Background sweeps across every data plane (ADR 0021)."""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.conversations.sweeper import StaleReplySweeper
from documind.application.infrastructure.resolver import InfrastructureResolver
from documind.application.ports import EmailSender, JobDispatcher
from documind.application.processing.sweeper import ProcessingSweeper, SweepResult
from documind.core.config import Settings
from documind.infrastructure.database.dataplane_runtime import DataPlane, relay_outbox

logger = logging.getLogger(__name__)


class _NoDispatch:
    async def dispatch(
        self,
        job_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID | None = None,
        countdown_seconds: int | None = None,
    ) -> bool:
        return True


async def _planes(resolver: InfrastructureResolver) -> list[DataPlane]:
    return [resolver.shared, *(plane for plane, _ in await resolver.isolated_planes())]


async def sweep_processing(
    resolver: InfrastructureResolver, settings: Settings, dispatcher: JobDispatcher
) -> SweepResult:
    total = SweepResult(0, 0, 0, 0)
    for plane in await _planes(resolver):
        # Customer-processed planes: recover leases and create missing jobs, but never
        # hand their jobs to DocuMind's workers (the customer's workers claim them).
        target = _NoDispatch() if plane.processing_mode == "customer" else dispatcher
        try:
            result = await ProcessingSweeper(plane.sessions, settings, target).sweep()
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


async def sweep_extractions(
    resolver: InfrastructureResolver,
    dispatch: Callable[[uuid.UUID, uuid.UUID], Awaitable[bool]],
    *,
    stale_seconds: int,
) -> int:
    from documind.application.extraction.reminders import recover_extractions

    changed = 0
    for plane in await _planes(resolver):
        try:
            changed += await recover_extractions(
                plane.sessions, dispatch, stale_seconds=stale_seconds
            )
        except Exception:
            logger.exception("extraction sweep failed for a data plane", extra={"plane": plane.key})
    return changed


async def send_deadline_reminders(
    resolver: InfrastructureResolver,
    control: async_sessionmaker[AsyncSession],
    email: EmailSender,
    base_url: str,
) -> int:
    from documind.application.extraction.reminders import send_reminders

    sent = 0
    for plane in await _planes(resolver):
        try:
            sent += await send_reminders(plane.sessions, control, email, base_url)
        except Exception:
            logger.exception(
                "deadline reminders failed for a data plane", extra={"plane": plane.key}
            )
    return sent
