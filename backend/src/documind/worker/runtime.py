"""Per-process runtime for Celery workers.

Each worker process lazily creates one event loop and one container, so
database and Redis connection pools are reused across tasks (and never shared
across forked processes).
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import Coroutine
from typing import Any

from documind.application.infrastructure.migration import MigrationExecutor
from documind.application.infrastructure.resolver import InfrastructureTargets
from documind.application.infrastructure.sweeps import sweep_processing, sweep_replies
from documind.application.processing.orchestrator import ProcessingOrchestrator
from documind.container import Container, build_container, processing_ai
from documind.core.config import get_settings

_loop: asyncio.AbstractEventLoop | None = None
_container: Container | None = None


def _state() -> tuple[asyncio.AbstractEventLoop, Container]:
    global _loop, _container
    if _loop is None or _container is None:
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _container = build_container(get_settings())
    return _loop, _container


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    loop, _ = _state()
    return loop.run_until_complete(coro)


def container() -> Container:
    return _state()[1]


def orchestrator() -> ProcessingOrchestrator:
    c = container()
    return ProcessingOrchestrator(
        c.session_factory,
        c.settings,
        c.storage,
        c.extractor,
        worker=f"{socket.gethostname()}:{os.getpid()}",
        ai=processing_ai(c),
        targets=InfrastructureTargets(c.infrastructure()),
    )


async def sweep() -> object:
    c = container()
    return await sweep_processing(c.infrastructure(), c.settings, c.job_dispatcher)


async def run_extraction(run_id: uuid.UUID, workspace_id: uuid.UUID) -> str:
    """One extraction run on the workspace's own data plane, with its own AI settings."""
    from documind.application.extraction.runner import ExtractionRunner

    c = container()
    infra = await c.infrastructure().resolve(workspace_id)

    async def llm_for(ws: uuid.UUID, user: uuid.UUID | None) -> Any:
        return (await c.ai_resolver().resolve(ws, user_id=user)).llm

    return await ExtractionRunner(infra.data_plane.sessions, llm_for, c.settings).run(run_id)


async def sweep_extractions() -> int:
    from documind.application.infrastructure.sweeps import sweep_extractions as sweep

    c = container()
    return await sweep(
        c.infrastructure(), c.extraction(), stale_seconds=c.settings.extraction_stale_seconds
    )


async def deadline_reminders() -> int:
    from documind.application.infrastructure.sweeps import send_deadline_reminders

    c = container()
    return await send_deadline_reminders(
        c.infrastructure(), c.session_factory, c.email_sender, c.settings.public_app_url
    )


class _NoDispatch:
    async def dispatch(self, job_id: uuid.UUID, **_: Any) -> bool:
        return True


async def connector_data(workspace_id: uuid.UUID) -> Any:
    """The workspace's data plane and a document service on it (connector imports)."""
    from documind.application.connectors.sync import DataAccess
    from documind.application.documents.document_service import DocumentService
    from documind.application.identity.common import RequestMeta

    c = container()
    infra = await c.infrastructure().resolve(workspace_id)
    dispatcher: Any = (
        _NoDispatch() if infra.config.processing.mode == "customer" else c.job_dispatcher
    )
    return DataAccess(
        infra.data_plane.sessions,
        lambda s: DocumentService(
            s, c.settings, infra.storage, c.malware_scanner, RequestMeta(), dispatcher
        ),
    )


async def sync_connector(connection_id: uuid.UUID) -> str:
    from documind.application.connectors.sync import DriveSync

    c = container()
    if c.drive_client is None:
        return "not_configured"
    lock = f"connector-sync:{connection_id}"
    if not await c.redis.set(lock, "1", nx=True, ex=1800):
        return "already_running"
    try:
        report = await DriveSync(
            c.session_factory,
            c.drive_client,
            c.cipher,
            connector_data,
            max_files=c.settings.connector_max_files_per_sync,
            max_bytes=c.settings.max_upload_bytes,
        ).run(connection_id)
        return report.status
    finally:
        await c.redis.delete(lock)


async def schedule_connector_syncs() -> int:
    from datetime import timedelta

    from sqlalchemy import or_, select

    from documind.application.identity.common import utcnow
    from documind.infrastructure.database.models import ConnectorConnection

    c = container()
    if c.drive_client is None:
        return 0
    due = utcnow() - timedelta(seconds=c.settings.connector_sync_interval_seconds)
    async with c.session_factory() as s:
        rows = (
            await s.execute(
                select(ConnectorConnection.id, ConnectorConnection.workspace_id).where(
                    ConnectorConnection.status.in_(("active", "paused")),
                    or_(
                        ConnectorConnection.last_sync_at.is_(None),
                        ConnectorConnection.last_sync_at < due,
                    ),
                )
            )
        ).all()
    for connection_id, workspace_id in rows:
        await c.connectors()(connection_id, workspace_id)
    return len(rows)


async def sync_seats() -> int:
    """Keep Stripe subscription quantities equal to member counts (ADR 0032)."""
    from documind.application.billing.subscriptions import SubscriptionService
    from documind.container import plan_changed

    c = container()
    if c.billing_gateway is None:
        return 0
    async with c.session_factory() as session:
        return await SubscriptionService(
            session,
            c.settings,
            c.billing_gateway,
            email=c.email_sender,
            on_plan_change=plan_changed(c),
        ).sync_seats()


async def sweep_replies_all() -> int:
    c = container()
    return await sweep_replies(
        c.infrastructure(), stale_after_seconds=c.settings.chat_stream_stale_seconds
    )


async def run_migration(migration_id: uuid.UUID) -> dict[str, object]:
    c = container()
    executor = MigrationExecutor(c.session_factory, c.infrastructure())
    return (await executor.run(migration_id)).as_dict()
