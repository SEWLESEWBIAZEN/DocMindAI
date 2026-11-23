"""Celery app factory and task names (importable without building the app)."""

from __future__ import annotations

from celery import Celery

from documind.core.config import Settings

QUEUE = "documents"
PROCESS_TASK = "documind.process_document"
SWEEP_TASK = "documind.sweep_processing_jobs"


def create_celery(settings: Settings) -> Celery:
    app = Celery("documind", broker=settings.broker_url, include=["documind.worker.tasks"])
    app.conf.update(
        task_default_queue=QUEUE,
        task_ignore_result=True,
        # Acknowledge only after the task finishes; redeliver if the worker dies.
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_soft_time_limit=settings.processing_lease_seconds - 120,
        task_time_limit=settings.processing_lease_seconds - 60,
        # Longer than any task, so Redis doesn't redeliver running tasks.
        broker_transport_options={"visibility_timeout": settings.processing_lease_seconds + 600},
        broker_connection_retry_on_startup=True,
        worker_hijack_root_logger=False,
        task_serializer="json",
        accept_content=["json"],
        beat_schedule={
            "sweep-processing-jobs": {
                "task": SWEEP_TASK,
                "schedule": float(settings.processing_sweep_interval_seconds),
                "options": {"queue": QUEUE, "expires": settings.processing_sweep_interval_seconds},
            }
        },
    )
    return app
