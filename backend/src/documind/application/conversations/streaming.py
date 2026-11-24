"""Runs one answer from the model to the database, as a stream of events.

The model runs in a producer task feeding a queue. The consumer (this
generator, iterated by the HTTP response) forwards deltas, emits heartbeats
while the model is silent, polls for cancel requests and keeps the message's
database heartbeat fresh. However the stream ends, the message ends in exactly
one terminal state:

- ``completed`` (verified answer + citations) when the model finished;
- ``cancelled`` when the user asked to stop (``cancelled_by_user``) or the
  client went away (``client_disconnected``);
- ``failed`` when the model or the pipeline failed.

Incomplete answers are never marked completed. The finalization on disconnect
is shielded from cancellation; if the process itself dies, the stale-stream
sweeper fails the message later.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass

import anyio

from documind.application.conversations.finalizer import ReplyFinalizer
from documind.application.conversations.views import MessageInfo
from documind.application.ports import CancellationRegistry
from documind.application.rag.orchestrator import (
    AnswerCompleted,
    AnswerDelta,
    AnswerEvent,
    PreparedAnswer,
    RAGOrchestrator,
)
from documind.domain.errors import DomainError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReplyStarted:
    user_message: MessageInfo | None  # None when regenerating
    assistant_message: MessageInfo


@dataclass(frozen=True, slots=True)
class ReplyDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ReplyHeartbeat:
    pass


@dataclass(frozen=True, slots=True)
class ReplyCompleted:
    message: MessageInfo


@dataclass(frozen=True, slots=True)
class ReplyCancelled:
    message: MessageInfo | None


@dataclass(frozen=True, slots=True)
class ReplyFailed:
    message: MessageInfo | None
    code: str
    detail: str


ReplyEvent = (
    ReplyStarted | ReplyDelta | ReplyHeartbeat | ReplyCompleted | ReplyCancelled | ReplyFailed
)


@dataclass(frozen=True, slots=True)
class StreamOptions:
    heartbeat_seconds: float = 15.0
    cancel_poll_seconds: float = 0.5
    touch_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class _Failure:
    error: BaseException


_DONE = object()


class ReplyRunner:
    def __init__(
        self,
        orchestrator: RAGOrchestrator,
        finalizer: ReplyFinalizer,
        cancellation: CancellationRegistry,
        options: StreamOptions,
    ) -> None:
        self._orchestrator = orchestrator
        self._finalizer = finalizer
        self._cancellation = cancellation
        self._options = options

    async def run(
        self, prepared: PreparedAnswer, started: ReplyStarted
    ) -> AsyncGenerator[ReplyEvent]:
        message_id = started.assistant_message.id
        queue: asyncio.Queue[object] = asyncio.Queue()

        async def produce() -> None:
            try:
                events: AsyncIterator[AnswerEvent] = self._orchestrator.stream_prepared(prepared)
                async for event in events:
                    await queue.put(event)
                await queue.put(_DONE)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # forwarded to the consumer
                await queue.put(_Failure(exc))

        producer = asyncio.create_task(produce(), name=f"answer-{message_id}")
        partial: list[str] = []
        finished = False
        loop = asyncio.get_running_loop()
        opts = self._options
        last_event = last_poll = last_touch = loop.time()
        try:
            yield started
            while True:
                timeout = max(0.0, min(opts.cancel_poll_seconds, opts.heartbeat_seconds))
                try:
                    item: object | None = await asyncio.wait_for(queue.get(), timeout)
                except TimeoutError:
                    item = None
                now = loop.time()
                if now - last_poll >= opts.cancel_poll_seconds:
                    last_poll = now
                    if await self._cancellation.is_requested(message_id):
                        finished = True
                        message = await self._stop(
                            message_id, partial, "cancelled", "cancelled_by_user"
                        )
                        yield ReplyCancelled(message)
                        return
                if now - last_touch >= opts.touch_seconds:
                    last_touch = now
                    await self._finalizer.touch(message_id)
                if item is None:
                    if now - last_event >= opts.heartbeat_seconds:
                        last_event = now
                        yield ReplyHeartbeat()
                    continue
                last_event = now
                if isinstance(item, AnswerDelta):
                    partial.append(item.text)
                    yield ReplyDelta(item.text)
                elif isinstance(item, AnswerCompleted):
                    finished = True
                    with anyio.CancelScope(shield=True):  # never half-write an answer
                        completed = await self._finalizer.complete(message_id, item.result)
                    if completed is None:
                        yield ReplyFailed(
                            None,
                            "reply_interrupted",
                            "The answer was interrupted. Please ask again.",
                        )
                    else:
                        yield ReplyCompleted(completed)
                    return
                elif isinstance(item, _Failure):
                    finished = True
                    code, detail = self._describe(item.error)
                    message = await self._stop(message_id, partial, "failed", code)
                    yield ReplyFailed(message, code, detail)
                    return
                else:  # _DONE without a completion: treat as a failure
                    finished = True
                    message = await self._stop(message_id, partial, "failed", "no_answer")
                    yield ReplyFailed(message, "no_answer", "No answer was produced.")
                    return
        finally:
            producer.cancel()
            with anyio.CancelScope(shield=True):
                with contextlib.suppress(BaseException):
                    await producer
                if not finished:
                    # The client disconnected (or the response was torn down).
                    try:
                        await self._finalizer.stop(
                            message_id,
                            "".join(partial),
                            status="cancelled",
                            error_code="client_disconnected",
                        )
                    except Exception:
                        logger.exception("could not record an interrupted answer")
                await self._cancellation.clear(message_id)

    async def _stop(
        self, message_id: uuid.UUID, partial: list[str], status: str, code: str
    ) -> MessageInfo | None:
        message: MessageInfo | None = None
        with anyio.CancelScope(shield=True):
            message = await self._finalizer.stop(
                message_id, "".join(partial), status=status, error_code=code
            )
        return message

    @staticmethod
    def _describe(error: BaseException) -> tuple[str, str]:
        if isinstance(error, DomainError):
            return error.code, error.message
        logger.error("answer generation failed", exc_info=error)
        return "internal_error", "Something went wrong while answering. Please try again."
