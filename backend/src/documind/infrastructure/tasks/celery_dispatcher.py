"""JobDispatcher that publishes to the Celery queue."""

from __future__ import annotations

import asyncio
import logging
import uuid

from celery import Celery

logger = logging.getLogger(__name__)


class CeleryJobDispatcher:
    def __init__(self, app: Celery, *, task_name: str, queue: str) -> None:
        self._app = app
        self._task = task_name
        self._queue = queue

    async def dispatch(self, job_id: uuid.UUID, *, countdown_seconds: int | None = None) -> bool:
        try:
            await asyncio.to_thread(
                self._app.send_task,
                self._task,
                args=[str(job_id)],
                queue=self._queue,
                countdown=countdown_seconds,
            )
        except Exception:
            # The job row is durable; the sweeper will dispatch it again.
            logger.exception("Failed to publish processing job", extra={"job_id": str(job_id)})
            return False
        return True
