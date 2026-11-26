"""Celery app factory and task names (importable without building the app)."""

from __future__ import annotations

from celery import Celery

from documind.core.config import Settings

QUEUE = "documents"
PROCESS_TASK = "documind.process_document"
SWEEP_TASK = "documind.sweep_processing_jobs"
REPLY_SWEEP_TASK = "documind.sweep_stale_replies"
MIGRATION_TASK = "documind.run_infrastructure_migration"
SEAT_SYNC_TASK = "documind.sync_billing_seats"
EXTRACTION_TASK = "documind.run_extraction"
EXTRACTION_SWEEP_TASK = "documind.sweep_extractions"
REMINDER_TASK = "documind.send_deadline_reminders"
CONNECTOR_SYNC_TASK = "documind.sync_connector"
CONNECTOR_SCHEDULE_TASK = "documind.schedule_connector_syncs"
SLACK_ANSWER_TASK = "documind.slack_answer"
DATA_EXPORT_TASK = "documind.build_data_export"
EXPORT_SWEEP_TASK = "documind.sweep_data_exports"


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
            },
            # Answers whose API process died mid-stream (no heartbeat).
            "sweep-stale-replies": {
                "task": REPLY_SWEEP_TASK,
                "schedule": 60.0,
                "options": {"queue": QUEUE, "expires": 60},
            },
            # Extraction runs whose worker stopped, or whose message was lost (ADR 0034).
            "sweep-extractions": {
                "task": EXTRACTION_SWEEP_TASK,
                "schedule": 120.0,
                "options": {"queue": QUEUE, "expires": 120},
            },
            # Connectors due for a sync (ADR 0035). No-op when none are connected.
            "connector-syncs": {
                "task": CONNECTOR_SCHEDULE_TASK,
                "schedule": 300.0,
                "options": {"queue": QUEUE, "expires": 300},
            },
            "deadline-reminders": {
                "task": REMINDER_TASK,
                "schedule": float(settings.deadline_reminder_interval_seconds),
                "options": {"queue": QUEUE, "expires": settings.deadline_reminder_interval_seconds},
            },
            # Expired "download my data" archives (ADR 0039).
            "sweep-data-exports": {
                "task": EXPORT_SWEEP_TASK,
                "schedule": 3600.0,
                "options": {"queue": QUEUE, "expires": 3600},
            },
            # Subscription quantity follows the member count (ADR 0032). No-op without Stripe.
            "sync-billing-seats": {
                "task": SEAT_SYNC_TASK,
                "schedule": float(settings.billing_seat_sync_interval_seconds),
                "options": {"queue": QUEUE, "expires": settings.billing_seat_sync_interval_seconds},
            },
        },
    )
    return app
