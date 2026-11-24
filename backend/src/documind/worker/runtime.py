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

from documind.application.conversations.sweeper import StaleReplySweeper
from documind.application.processing.embedding_service import EmbeddingService
from documind.application.processing.orchestrator import ProcessingOrchestrator
from documind.application.processing.sweeper import ProcessingSweeper
from documind.container import Container, build_container
from documind.core.config import get_settings
from documind.infrastructure.database.repositories.processing import SessionEmbeddingCache

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
    embeddings = EmbeddingService(
        c.embedding_provider,
        SessionEmbeddingCache(c.session_factory),
        batch_size=c.settings.embedding_batch_size,
    )
    return ProcessingOrchestrator(
        c.session_factory,
        c.settings,
        c.storage,
        c.extractor,
        embeddings,
        worker=f"{socket.gethostname()}:{os.getpid()}",
    )


def sweeper() -> ProcessingSweeper:
    c = container()
    return ProcessingSweeper(c.session_factory, c.settings, c.job_dispatcher)


def reply_sweeper() -> StaleReplySweeper:
    c = container()
    return StaleReplySweeper(
        c.session_factory, stale_after_seconds=c.settings.chat_stream_stale_seconds
    )
