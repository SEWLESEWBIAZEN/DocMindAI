"""Fails answers whose generating process died (no heartbeat for too long)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.identity.common import utcnow
from documind.infrastructure.database.repositories.conversations import MessageRepository


class StaleReplySweeper:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, stale_after_seconds: int
    ) -> None:
        self._sessions = session_factory
        self._stale = timedelta(seconds=stale_after_seconds)

    async def sweep(self) -> int:
        async with self._sessions() as session:
            failed = await MessageRepository(session).fail_stale(utcnow() - self._stale)
            await session.commit()
            return failed
