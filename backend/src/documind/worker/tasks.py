"""Celery tasks. Thin: all logic lives in the orchestrator and sweeper."""

from __future__ import annotations

import logging
import uuid

from documind.worker import runtime
from documind.worker.celery_app import PROCESS_TASK, QUEUE, SWEEP_TASK, app

logger = logging.getLogger(__name__)


@app.task(name=PROCESS_TASK, acks_late=True, reject_on_worker_lost=True, ignore_result=True)  # type: ignore[untyped-decorator]
def process_document(job_id: str) -> str:
    outcome = runtime.run(runtime.orchestrator().run(uuid.UUID(job_id)))
    if outcome.status == "retry" and outcome.retry_in_seconds is not None:
        process_document.apply_async(args=[job_id], countdown=outcome.retry_in_seconds, queue=QUEUE)
    return outcome.status


@app.task(name=SWEEP_TASK, ignore_result=True)  # type: ignore[untyped-decorator]
def sweep_processing_jobs() -> None:
    runtime.run(runtime.sweeper().sweep())
