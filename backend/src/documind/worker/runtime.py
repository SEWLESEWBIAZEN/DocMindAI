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
