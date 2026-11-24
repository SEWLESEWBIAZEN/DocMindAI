import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from documind.application.conversations.streaming import (
    ReplyCancelled,
    ReplyCompleted,
    ReplyDelta,
    ReplyEvent,
    ReplyFailed,
    ReplyHeartbeat,
    ReplyRunner,
    ReplyStarted,
    StreamOptions,
)
from documind.application.conversations.views import MessageInfo
from documind.application.rag.orchestrator import (
    AnswerCompleted,
    AnswerDelta,
    AnswerEvent,
    AnswerUsage,
    RagAnswer,
)
from documind.domain.errors import ServiceUnavailableError
from documind.infrastructure.cache.cancellation import InMemoryCancellationRegistry

MID = uuid.uuid4()


def info(status: str = "streaming", content: str = "", code: str | None = None) -> MessageInfo:
    return MessageInfo(
        id=MID,
        conversation_id=uuid.uuid4(),
        sequence=2,
        role="assistant",
        content=content,
        status=status,
        reply_to_id=uuid.uuid4(),
        answer_status=None,
        model=None,
        prompt_version=None,
        retrieval_mode=None,
        input_tokens=0,
        output_tokens=0,
        embedding_tokens=0,
        latency_ms=None,
        warnings=(),
        error_code=code,
        superseded=False,
        created_at=datetime.now(UTC),
        completed_at=None,
    )


RESULT = RagAnswer(
    answer="Ninety days [1].",
    status="grounded",
    citations=(),
    sources=(),
    warnings=(),
    model="m",
    prompt_version="v",
    retrieval_mode="hybrid",
    usage=AnswerUsage(1, 2, 3),
)


class FakeFinalizer:
    def __init__(self, complete_returns: bool = True) -> None:
        self.completed: list[RagAnswer] = []
        self.stopped: list[tuple[str, str, str]] = []
        self.touched = 0
        self._complete_returns = complete_returns

    async def complete(self, message_id: uuid.UUID, result: RagAnswer) -> MessageInfo | None:
        self.completed.append(result)
        return info("completed", result.answer) if self._complete_returns else None

    async def stop(
        self, message_id: uuid.UUID, partial: str, *, status: str, error_code: str
    ) -> MessageInfo | None:
        self.stopped.append((partial, status, error_code))
        return info(status, partial, error_code)

    async def touch(self, message_id: uuid.UUID) -> bool:
        self.touched += 1
        return True


class ScriptedOrchestrator:
    def __init__(self, script: list[Any], delay: float = 0.0) -> None:
        self.script = script
        self.delay = delay
        self.closed = False

    async def stream_prepared(self, prepared: Any) -> AsyncIterator[AnswerEvent]:
        try:
            for step in self.script:
                if self.delay:
                    await asyncio.sleep(self.delay)
                if isinstance(step, BaseException):
                    raise step
                yield step
        finally:
            self.closed = True


def runner(
    orchestrator: ScriptedOrchestrator,
    finalizer: FakeFinalizer,
    cancellation: InMemoryCancellationRegistry | None = None,
    **options: float,
) -> ReplyRunner:
    return ReplyRunner(
        cast(Any, orchestrator),
        cast(Any, finalizer),
        cancellation or InMemoryCancellationRegistry(),
        StreamOptions(**{"heartbeat_seconds": 5.0, "cancel_poll_seconds": 0.01, **options}),
    )


STARTED = ReplyStarted(None, info())


async def events_of(stream: AsyncIterator[ReplyEvent]) -> list[ReplyEvent]:
    return [e async for e in stream]


async def test_completes_and_persists() -> None:
    fin = FakeFinalizer()
    script = [AnswerDelta("Ninety "), AnswerDelta("days."), AnswerCompleted(RESULT)]
    events = await events_of(
        runner(ScriptedOrchestrator(script), fin).run(cast(Any, None), STARTED)
    )
    assert isinstance(events[0], ReplyStarted)
    assert [e.text for e in events if isinstance(e, ReplyDelta)] == ["Ninety ", "days."]
    assert isinstance(events[-1], ReplyCompleted)
    assert fin.completed == [RESULT]
    assert fin.stopped == []


async def test_user_cancel_keeps_partial_and_is_never_completed() -> None:
    fin = FakeFinalizer()
    cancellation = InMemoryCancellationRegistry()
    script = [AnswerDelta(f"w{i} ") for i in range(50)] + [AnswerCompleted(RESULT)]
    orchestrator = ScriptedOrchestrator(script, delay=0.01)
    seen: list[ReplyEvent] = []
    async for event in runner(orchestrator, fin, cancellation).run(cast(Any, None), STARTED):
        seen.append(event)
        if isinstance(event, ReplyDelta) and event.text == "w2 ":
            await cancellation.request(MID)
    assert isinstance(seen[-1], ReplyCancelled)
    assert fin.completed == []
    partial, status, code = fin.stopped[0]
    assert (status, code) == ("cancelled", "cancelled_by_user")
    assert partial.startswith("w0 w1 w2 ")
    assert orchestrator.closed  # generation stopped
    assert MID not in cancellation.requested  # flag cleared


async def test_model_failure_marks_failed() -> None:
    fin = FakeFinalizer()
    script = [AnswerDelta("Partial "), ServiceUnavailableError("down", code="llm_unavailable")]
    events = await events_of(
        runner(ScriptedOrchestrator(script), fin).run(cast(Any, None), STARTED)
    )
    failed = events[-1]
    assert isinstance(failed, ReplyFailed)
    assert failed.code == "llm_unavailable"
    assert fin.stopped == [("Partial ", "failed", "llm_unavailable")]


async def test_unexpected_error_is_generic() -> None:
    fin = FakeFinalizer()
    events = await events_of(
        runner(ScriptedOrchestrator([RuntimeError("secret detail")]), fin).run(
            cast(Any, None), STARTED
        )
    )
    failed = events[-1]
    assert isinstance(failed, ReplyFailed)
    assert failed.code == "internal_error"
    assert "secret" not in failed.detail


async def test_stream_ending_without_completion_fails() -> None:
    fin = FakeFinalizer()
    events = await events_of(
        runner(ScriptedOrchestrator([AnswerDelta("x")]), fin).run(cast(Any, None), STARTED)
    )
    assert isinstance(events[-1], ReplyFailed)
    assert fin.stopped[0][1:] == ("failed", "no_answer")


async def test_client_disconnect_records_cancelled() -> None:
    fin = FakeFinalizer()
    script = [AnswerDelta(f"w{i} ") for i in range(50)] + [AnswerCompleted(RESULT)]
    orchestrator = ScriptedOrchestrator(script, delay=0.01)
    stream = runner(orchestrator, fin).run(cast(Any, None), STARTED)
    async for event in stream:
        if isinstance(event, ReplyDelta):
            break
    await stream.aclose()  # what the server does when the client goes away
    assert fin.completed == []
    assert fin.stopped == [("w0 ", "cancelled", "client_disconnected")]
    assert orchestrator.closed


async def test_task_cancellation_records_cancelled() -> None:
    fin = FakeFinalizer()
    orchestrator = ScriptedOrchestrator([AnswerDelta("a "), AnswerCompleted(RESULT)], delay=10)

    async def consume() -> None:
        async for _ in runner(orchestrator, fin).run(cast(Any, None), STARTED):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fin.stopped == [("", "cancelled", "client_disconnected")]


async def test_heartbeats_and_touches_while_model_is_silent() -> None:
    fin = FakeFinalizer()
    orchestrator = ScriptedOrchestrator([AnswerCompleted(RESULT)], delay=0.2)
    events = await events_of(
        runner(orchestrator, fin, heartbeat_seconds=0.05, touch_seconds=0.05).run(
            cast(Any, None), STARTED
        )
    )
    assert sum(isinstance(e, ReplyHeartbeat) for e in events) >= 2
    assert fin.touched >= 2
    assert isinstance(events[-1], ReplyCompleted)


async def test_completion_after_sweep_is_reported_as_interrupted() -> None:
    fin = FakeFinalizer(complete_returns=False)
    events = await events_of(
        runner(ScriptedOrchestrator([AnswerCompleted(RESULT)]), fin).run(cast(Any, None), STARTED)
    )
    failed = events[-1]
    assert isinstance(failed, ReplyFailed)
    assert failed.code == "reply_interrupted"
