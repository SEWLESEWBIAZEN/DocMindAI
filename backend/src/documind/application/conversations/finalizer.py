"""Writes the outcome of a streamed answer in its own short transaction.

Streaming outlives the request's database session, so every write here opens
a fresh session. All writes are conditional on the message still being
``streaming``: an answer that the stale-stream sweeper already failed, or
whose conversation was deleted, is never resurrected.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.conversations.views import (
    MessageInfo,
    citation_info,
    message_info,
)
from documind.application.identity.common import utcnow
from documind.application.rag.orchestrator import RagAnswer
from documind.application.rag.retrieval import SearchWarning
from documind.infrastructure.database.models import Message, MessageCitation
from documind.infrastructure.database.repositories.conversations import (
    ConversationRepository,
    MessageRepository,
)


def _warning(w: SearchWarning) -> dict[str, Any]:
    out: dict[str, Any] = {"code": w.code, "message": w.message}
    if w.document_id:
        out["document_id"] = str(w.document_id)
    if w.count is not None:
        out["count"] = w.count
    return out


class ReplyFinalizer:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def _bump(self, session: AsyncSession, message: Message, now: datetime) -> None:
        conversation = await ConversationRepository(session).get(message.conversation_id)
        if conversation is not None:
            conversation.last_message_at = now

    async def complete(self, message_id: uuid.UUID, result: RagAnswer) -> MessageInfo | None:
        async with self._sessions() as session:
            messages = MessageRepository(session)
            message = await messages.get_for_update(message_id)
            if message is None or message.status != "streaming":
                return None
            now = utcnow()
            message.content = result.answer
            message.status = "completed"
            message.answer_status = result.status
            message.model = result.model
            message.prompt_version = result.prompt_version
            message.retrieval_mode = result.retrieval_mode
            message.retrieval_query = result.retrieval_query or message.retrieval_query
            message.input_tokens = result.usage.input_tokens
            message.output_tokens = result.usage.output_tokens
            message.embedding_tokens = result.usage.embedding_tokens
            message.latency_ms = result.latency_ms
            message.warnings = [_warning(w) for w in result.warnings]
            message.error_code = None
            message.completed_at = now
            # A chunk can be replaced by re-processing while the answer streams.
            live_chunks = await messages.existing_chunk_ids([c.chunk_id for c in result.citations])
            rows = [
                MessageCitation(
                    id=uuid.uuid4(),
                    workspace_id=message.workspace_id,
                    message_id=message.id,
                    number=c.number,
                    document_id=c.document_id,
                    chunk_id=c.chunk_id if c.chunk_id in live_chunks else None,
                    document_name=c.document_name[:255],
                    page_start=c.page_start,
                    page_end=c.page_end,
                    heading_path=list(c.heading_path),
                    excerpt=c.excerpt,
                    label=c.label[:400],
                    spoken_label=c.spoken_label[:400],
                )
                for c in result.citations
            ]
            for row in rows:
                messages.add_citation(row)
            await self._bump(session, message, now)
            await session.commit()
            await session.refresh(message)
            return message_info(message, tuple(citation_info(r, True) for r in rows))

    async def stop(
        self, message_id: uuid.UUID, partial: str, *, status: str, error_code: str
    ) -> MessageInfo | None:
        """End an answer as ``cancelled`` or ``failed``, keeping what was streamed."""
        async with self._sessions() as session:
            message = await MessageRepository(session).get_for_update(message_id)
            if message is None or message.status != "streaming":
                return None
            now = utcnow()
            message.status = status
            message.content = partial
            message.error_code = error_code
            message.completed_at = now
            await self._bump(session, message, now)
            await session.commit()
            await session.refresh(message)
            return message_info(message)

    async def touch(self, message_id: uuid.UUID) -> bool:
        """Heartbeat: tells the stale-stream sweeper this answer is still alive."""
        async with self._sessions() as session:
            alive = await MessageRepository(session).touch(message_id)
            await session.commit()
            return alive
