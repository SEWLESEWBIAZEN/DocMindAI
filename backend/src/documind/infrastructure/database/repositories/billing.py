"""Plans, workspace plans and the usage ledger (control plane)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import Integer, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.ai.metering import UsageEvent
from documind.infrastructure.database.models import UsageRecord, WorkspacePlan

logger = logging.getLogger(__name__)


class WorkspacePlanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(
        self, workspace_id: uuid.UUID, *, for_update: bool = False
    ) -> WorkspacePlan | None:
        stmt = select(WorkspacePlan).where(WorkspacePlan.workspace_id == workspace_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._s.execute(stmt)).scalar_one_or_none()

    def add(self, row: WorkspacePlan) -> None:
        self._s.add(row)


class UsageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def totals(
        self, workspace_id: uuid.UUID, *, since: datetime, billed_to: str | None = None
    ) -> dict[str, dict[str, float]]:
        """Per capability: tokens, audio seconds, characters, units and calls."""
        filters = [UsageRecord.workspace_id == workspace_id, UsageRecord.occurred_at >= since]
        if billed_to is not None:
            filters.append(UsageRecord.billed_to == billed_to)
        rows = (
            await self._s.execute(
                select(
                    UsageRecord.capability,
                    UsageRecord.billed_to,
                    func.coalesce(func.sum(UsageRecord.input_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.output_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.audio_seconds), 0.0),
                    func.coalesce(func.sum(UsageRecord.characters), 0),
                    func.coalesce(func.sum(UsageRecord.units), 0),
                    func.count(),
                )
                .where(*filters)
                .group_by(UsageRecord.capability, UsageRecord.billed_to)
            )
        ).all()
        out: dict[str, dict[str, float]] = {}
        for cap, billed, inp, outp, secs, chars, units, calls in rows:
            out[f"{cap}:{billed}"] = {
                "input_tokens": float(inp),
                "output_tokens": float(outp),
                "audio_seconds": float(secs),
                "characters": float(chars),
                "units": float(units),
                "calls": float(calls),
            }
        return out

    async def daily(
        self, workspace_id: uuid.UUID, *, since: datetime
    ) -> list[tuple[datetime, str, int, float]]:
        day = func.date_trunc("day", UsageRecord.occurred_at)
        rows = (
            await self._s.execute(
                select(
                    day,
                    UsageRecord.capability,
                    func.coalesce(
                        func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens), 0
                    ).cast(Integer),
                    func.coalesce(func.sum(UsageRecord.audio_seconds), 0.0),
                )
                .where(UsageRecord.workspace_id == workspace_id, UsageRecord.occurred_at >= since)
                .group_by(day, UsageRecord.capability)
                .order_by(day)
            )
        ).all()
        return [(r[0], r[1], int(r[2]), float(r[3])) for r in rows]


class SqlUsageSink:
    """Writes usage events in their own short transaction (control plane).

    Recorded even if the surrounding request later fails: the provider has
    billed the call either way. Duplicate idempotency keys are ignored.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def record(self, event: UsageEvent) -> None:
        values = asdict(event)
        values["capability"] = event.capability.value
        values["billed_to"] = event.billed_to.value
        values["model"] = event.model[:100]
        try:
            async with self._sessions() as session:
                stmt = insert(UsageRecord).values(**values)
                if event.idempotency_key:
                    stmt = stmt.on_conflict_do_nothing(index_elements=["idempotency_key"])
                await session.execute(stmt)
                await session.commit()
        except Exception:
            # Never break the request; the structured log keeps the event for reconciliation.
            logger.exception(
                "failed to record usage",
                extra={k: str(v) for k, v in values.items() if k != "details"},
            )
