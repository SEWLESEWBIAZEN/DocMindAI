"""Celery tasks. Thin: all logic lives in the orchestrator and sweeper."""

from __future__ import annotations

import logging
import uuid

from documind.worker import runtime
from documind.worker.celery_app import (
    CONNECTOR_SCHEDULE_TASK,
    CONNECTOR_SYNC_TASK,
    EXTRACTION_SWEEP_TASK,
    EXTRACTION_TASK,
    MIGRATION_TASK,
    PROCESS_TASK,
    QUEUE,
    REMINDER_TASK,
    REPLY_SWEEP_TASK,
    SEAT_SYNC_TASK,
    SLACK_ANSWER_TASK,
    SWEEP_TASK,
    app,
)

logger = logging.getLogger(__name__)


@app.task(name=PROCESS_TASK, acks_late=True, reject_on_worker_lost=True, ignore_result=True)  # type: ignore[untyped-decorator]
def process_document(job_id: str, workspace_id: str | None = None) -> str:
    """``workspace_id`` selects the data plane; older messages without it use the shared one."""
    ws = uuid.UUID(workspace_id) if workspace_id else None
    outcome = runtime.run(runtime.orchestrator().run(uuid.UUID(job_id), workspace_id=ws))
    if outcome.status == "retry" and outcome.retry_in_seconds is not None:
        args = [job_id] if workspace_id is None else [job_id, workspace_id]
        process_document.apply_async(args=args, countdown=outcome.retry_in_seconds, queue=QUEUE)
    return outcome.status


@app.task(name=SWEEP_TASK, ignore_result=True)  # type: ignore[untyped-decorator]
def sweep_processing_jobs() -> None:
    runtime.run(runtime.sweep())


@app.task(name=REPLY_SWEEP_TASK, ignore_result=True)  # type: ignore[untyped-decorator]
def sweep_stale_replies() -> int:
    failed = runtime.run(runtime.sweep_replies_all())
    if failed:
        logger.warning("marked interrupted answers as failed", extra={"count": failed})
    return failed


# Not acks_late: a migration must never be redelivered half-done. A run whose worker died
# stays "running" until an operator aborts it (documind-manage migrations abort).
@app.task(  # type: ignore[untyped-decorator]
    name=MIGRATION_TASK, ignore_result=True, time_limit=12 * 3600, soft_time_limit=None
)
def run_infrastructure_migration(migration_id: str) -> str:
    report = runtime.run(runtime.run_migration(uuid.UUID(migration_id)))
    return str(report.get("rows", {}))


@app.task(name=SEAT_SYNC_TASK, ignore_result=True)  # type: ignore[untyped-decorator]
def sync_billing_seats() -> int:
    changed = runtime.run(runtime.sync_seats())
    if changed:
        logger.info("updated subscription seats", extra={"count": changed})
    return changed


# Not acks_late: a run picked up twice would be skipped anyway (it's no longer queued),
# and a run whose worker died is failed by the sweep, then started again by the user.
@app.task(name=EXTRACTION_TASK, ignore_result=True)  # type: ignore[untyped-decorator]
def run_extraction(run_id: str, workspace_id: str) -> str:
    return runtime.run(runtime.run_extraction(uuid.UUID(run_id), uuid.UUID(workspace_id)))


@app.task(name=EXTRACTION_SWEEP_TASK, ignore_result=True)  # type: ignore[untyped-decorator]
def sweep_extractions() -> int:
    return runtime.run(runtime.sweep_extractions())


@app.task(name=REMINDER_TASK, ignore_result=True)  # type: ignore[untyped-decorator]
def send_deadline_reminders() -> int:
    sent = runtime.run(runtime.deadline_reminders())
    if sent:
        logger.info("sent deadline reminders", extra={"count": sent})
    return sent


@app.task(name=CONNECTOR_SYNC_TASK, ignore_result=True)  # type: ignore[untyped-decorator]
def sync_connector(connection_id: str, workspace_id: str | None = None) -> str:
    del workspace_id  # routing is read from the connection itself
    return runtime.run(runtime.sync_connector(uuid.UUID(connection_id)))


@app.task(name=CONNECTOR_SCHEDULE_TASK, ignore_result=True)  # type: ignore[untyped-decorator]
def schedule_connector_syncs() -> int:
    return runtime.run(runtime.schedule_connector_syncs())


@app.task(name=SLACK_ANSWER_TASK, ignore_result=True)  # type: ignore[untyped-decorator]
def slack_answer(job: dict[str, object]) -> str:
    return runtime.run(runtime.slack_answer(dict(job)))
