"""Celery application (Redis broker; job state lives in PostgreSQL, not Celery).

Run: ``celery -A documind.worker.celery_app worker -Q documents`` and
``celery -A documind.worker.celery_app beat``.
"""

from __future__ import annotations

from typing import Any

from celery.signals import setup_logging

from documind.core.config import get_settings
from documind.core.logging import configure_logging
from documind.worker.celery_factory import (
    EXTRACTION_SWEEP_TASK,
    EXTRACTION_TASK,
    MIGRATION_TASK,
    PROCESS_TASK,
    QUEUE,
    REMINDER_TASK,
    REPLY_SWEEP_TASK,
    SEAT_SYNC_TASK,
    SWEEP_TASK,
    create_celery,
)

__all__ = [
    "EXTRACTION_SWEEP_TASK",
    "EXTRACTION_TASK",
    "MIGRATION_TASK",
    "PROCESS_TASK",
    "QUEUE",
    "REMINDER_TASK",
    "REPLY_SWEEP_TASK",
    "SEAT_SYNC_TASK",
    "SWEEP_TASK",
    "app",
]


@setup_logging.connect  # type: ignore[untyped-decorator]
def _setup_logging(**_: Any) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)


app = create_celery(get_settings())
