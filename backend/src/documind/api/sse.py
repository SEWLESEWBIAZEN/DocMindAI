"""Server-Sent Events encoding for answer streams.

Event types (``event:`` field; ``data:`` is JSON):

- ``message.created``  ``{user_message, assistant_message}`` (saved, answer pending)
- ``answer.delta``     ``{message_id, text}``
- ``answer.completed`` ``{message}`` with verified text and citations (replaces deltas)
- ``answer.cancelled`` ``{message}`` partial text, never marked completed
- ``answer.error``     ``{code, detail, message}``

Comment lines (``: keep-alive``) are sent while the model is silent so proxies
don't close the connection.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse

from documind.api.schemas.conversations import MessageResponse
from documind.application.conversations.service import StartedReply
from documind.application.conversations.streaming import (
    ReplyCancelled,
    ReplyCompleted,
    ReplyDelta,
    ReplyFailed,
    ReplyHeartbeat,
    ReplyStarted,
)

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",  # nginx: don't buffer the stream
}


def wants_event_stream(accept: str | None) -> bool:
    return bool(accept) and "text/event-stream" in (accept or "").lower()


def _event(name: str, data: Any) -> bytes:
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)
    return f"event: {name}\ndata: {payload}\n\n".encode()


def _message(m: Any) -> Any:
    return MessageResponse.of(m).model_dump(mode="json") if m is not None else None


async def encode(started: StartedReply) -> AsyncIterator[bytes]:
    message_id = str(started.assistant_message.id)
    async for event in started.events:
        if isinstance(event, ReplyStarted):
            yield _event(
                "message.created",
                {
                    "user_message": _message(event.user_message),
                    "assistant_message": _message(event.assistant_message),
                },
            )
        elif isinstance(event, ReplyDelta):
            yield _event("answer.delta", {"message_id": message_id, "text": event.text})
        elif isinstance(event, ReplyHeartbeat):
            yield b": keep-alive\n\n"
        elif isinstance(event, ReplyCompleted):
            yield _event("answer.completed", {"message": _message(event.message)})
        elif isinstance(event, ReplyCancelled):
            yield _event("answer.cancelled", {"message": _message(event.message)})
        elif isinstance(event, ReplyFailed):
            yield _event(
                "answer.error",
                {"code": event.code, "detail": event.detail, "message": _message(event.message)},
            )


def event_stream(started: StartedReply) -> StreamingResponse:
    return StreamingResponse(encode(started), media_type="text/event-stream", headers=SSE_HEADERS)
