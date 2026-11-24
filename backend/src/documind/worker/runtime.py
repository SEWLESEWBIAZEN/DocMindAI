"""Per-process runtime for Celery workers.

Each worker process lazily creates one event loop and one container, so
database and Redis connection pools are reused across tasks (and never shared
across forked processes).
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Coroutine
from typing import Any

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


async def sweep_replies_all() -> int:
    c = container()
    return await sweep_replies(
        c.infrastructure(), stale_after_seconds=c.settings.chat_stream_stale_seconds
    )
