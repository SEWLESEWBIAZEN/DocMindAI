"""Periodic recovery of the job queue (run by Celery beat).

1. Running jobs whose lease expired (worker crashed or was killed) are re-queued,
   or marked dead if they've used all attempts.
2. Pending documents without an active job get one (defensive; e.g. documents
   uploaded before the pipeline existed).
3. Queued jobs that are due but weren't handed to the queue (publishing failed
   after commit) or whose dispatch was lost are dispatched again.

Together with the job row written in the upload transaction, this guarantees a
committed document can't silently remain unprocessed.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.identity.common import utcnow
from documind.application.ports import JobDispatcher
from documind.application.processing.jobs import new_job
from documind.core.config import Settings
from documind.domain.documents.status import DocumentStatus
from documind.infrastructure.database.repositories.documents import DocumentRepository
from documind.infrastructure.database.repositories.processing import ProcessingJobRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SweepResult:
    requeued: int
    dead: int
    created: int
    dispatched: int


class ProcessingSweeper:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        dispatcher: JobDispatcher,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._dispatcher = dispatcher

    async def sweep(self) -> SweepResult:
        now = utcnow()
        requeued = dead = created = 0
        to_dispatch: list[tuple[uuid.UUID, uuid.UUID]] = []
        async with self._sessions() as session:
            jobs = ProcessingJobRepository(session)
            for job in await jobs.expired_leases(now):
                job.lease_expires_at = None
                job.stage = None
                if job.attempts >= job.max_attempts:
                    job.status, job.finished_at = "dead", now
                    job.error_code = "lease_expired"
                    job.last_error = "The worker stopped responding too many times."
                    doc = await DocumentRepository(session).get(job.workspace_id, job.document_id)
                    if doc is not None and doc.current_version_id == job.document_version_id:
                        doc.status = DocumentStatus.FAILED.value
                        doc.processing_error = job.last_error
                    dead += 1
                else:
                    job.status, job.next_attempt_at, job.dispatched_at = "queued", now, None
                    requeued += 1

            for doc in await jobs.documents_missing_jobs():
                jobs.add(new_job(doc, self._settings, now=now, requested_by=None, dispatched=False))
                created += 1
            await session.flush()

            for job in await jobs.due_for_dispatch(
                now, self._settings.processing_dispatch_grace_seconds
            ):
                job.dispatched_at = now
                to_dispatch.append((job.id, job.workspace_id))
            await session.commit()

        dispatched = 0
        for job_id, workspace_id in to_dispatch:
            if await self._dispatcher.dispatch(job_id, workspace_id=workspace_id):
                dispatched += 1
        result = SweepResult(requeued=requeued, dead=dead, created=created, dispatched=dispatched)
        if any((requeued, dead, created, dispatched)):
            logger.info(
                "Processing sweep",
                extra={
                    "requeued": requeued,
                    "dead": dead,
                    "created": created,
                    "dispatched": dispatched,
                },
            )
        return result
