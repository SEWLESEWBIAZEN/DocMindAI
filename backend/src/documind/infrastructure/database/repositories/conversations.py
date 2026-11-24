"""Conversation repositories. Every query is scoped by workspace (and owner)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from documind.domain.rag.models import Turn
from documind.infrastructure.database.models import (
    Conversation,
    ConversationCollection,
    ConversationDocument,
    Document,
    DocumentChunk,
    DocumentCollection,
    Message,
    MessageCitation,
)
from documind.infrastructure.database.repositories.documents import like_pattern


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    def add(self, conversation: Conversation) -> None:
        self._s.add(conversation)

    async def get_owned(
        self,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> Conversation | None:
        stmt = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == workspace_id,
            Conversation.created_by_user_id == user_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get(self, conversation_id: uuid.UUID) -> Conversation | None:
        return await self._s.get(Conversation, conversation_id)

    async def list_owned(
        self,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        query: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[Conversation], int]:
        where: list[Any] = [
            Conversation.workspace_id == workspace_id,
            Conversation.created_by_user_id == user_id,
        ]
        if query:
            where.append(Conversation.title.ilike(like_pattern(query.strip()), escape="\\"))
        total = (
            await self._s.execute(select(func.count()).select_from(Conversation).where(*where))
        ).scalar_one()
        rows = (
            await self._s.execute(
                select(Conversation)
                .where(*where)
                .order_by(Conversation.last_message_at.desc(), Conversation.id)
                .offset(offset)
                .limit(limit)
            )
        ).scalars()
        return list(rows), int(total)

    async def delete(self, conversation: Conversation) -> None:
        await self._s.delete(conversation)

    # --- source selection ------------------------------------------------------

    async def source_ids(
        self, workspace_id: uuid.UUID, conversation_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[list[uuid.UUID], list[uuid.UUID]]]:
        result: dict[uuid.UUID, tuple[list[uuid.UUID], list[uuid.UUID]]] = {
            cid: ([], []) for cid in conversation_ids
        }
        if not conversation_ids:
            return result
        docs = await self._s.execute(
            select(ConversationDocument.conversation_id, ConversationDocument.document_id)
            .where(
                ConversationDocument.workspace_id == workspace_id,
                ConversationDocument.conversation_id.in_(conversation_ids),
            )
            .order_by(ConversationDocument.added_at, ConversationDocument.document_id)
        )
        for cid, did in docs.all():
            result[cid][0].append(did)
        cols = await self._s.execute(
            select(ConversationCollection.conversation_id, ConversationCollection.collection_id)
            .where(
                ConversationCollection.workspace_id == workspace_id,
                ConversationCollection.conversation_id.in_(conversation_ids),
            )
            .order_by(ConversationCollection.added_at, ConversationCollection.collection_id)
        )
        for cid, col in cols.all():
            result[cid][1].append(col)
        return result

    async def replace_sources(
        self,
        conversation: Conversation,
        document_ids: Sequence[uuid.UUID],
        collection_ids: Sequence[uuid.UUID],
    ) -> None:
        ws, cid = conversation.workspace_id, conversation.id
        await self._s.execute(
            delete(ConversationDocument).where(
                ConversationDocument.workspace_id == ws, ConversationDocument.conversation_id == cid
            )
        )
        await self._s.execute(
            delete(ConversationCollection).where(
                ConversationCollection.workspace_id == ws,
                ConversationCollection.conversation_id == cid,
            )
        )
        for did in dict.fromkeys(document_ids):
            self._s.add(ConversationDocument(conversation_id=cid, document_id=did, workspace_id=ws))
        for col in dict.fromkeys(collection_ids):
            self._s.add(
                ConversationCollection(conversation_id=cid, collection_id=col, workspace_id=ws)
            )

    async def selected_documents(
        self, workspace_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, str, str, bool]]:
        rows = await self._s.execute(
            select(Document.id, Document.title, Document.status, Document.archived_at)
            .join(
                ConversationDocument,
                (ConversationDocument.document_id == Document.id)
                & (ConversationDocument.workspace_id == Document.workspace_id),
            )
            .where(
                ConversationDocument.workspace_id == workspace_id,
                ConversationDocument.conversation_id == conversation_id,
            )
            .order_by(ConversationDocument.added_at, Document.id)
        )
        return [(r.id, r.title, r.status, r.archived_at is not None) for r in rows.all()]

    async def selected_collections(
        self, workspace_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, str]]:
        rows = await self._s.execute(
            select(DocumentCollection.id, DocumentCollection.name)
            .join(
                ConversationCollection,
                (ConversationCollection.collection_id == DocumentCollection.id)
                & (ConversationCollection.workspace_id == DocumentCollection.workspace_id),
            )
            .where(
                ConversationCollection.workspace_id == workspace_id,
                ConversationCollection.conversation_id == conversation_id,
            )
            .order_by(ConversationCollection.added_at, DocumentCollection.id)
        )
        return [(r.id, r.name) for r in rows.all()]


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    def add(self, message: Message) -> None:
        self._s.add(message)

    def add_citation(self, citation: MessageCitation) -> None:
        self._s.add(citation)

    async def get(
        self, workspace_id: uuid.UUID, conversation_id: uuid.UUID, message_id: uuid.UUID
    ) -> Message | None:
        stmt = select(Message).where(
            Message.id == message_id,
            Message.workspace_id == workspace_id,
            Message.conversation_id == conversation_id,
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_for_update(self, message_id: uuid.UUID) -> Message | None:
        stmt = select(Message).where(Message.id == message_id).with_for_update()
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def max_sequence(self, conversation_id: uuid.UUID) -> int:
        stmt = select(func.coalesce(func.max(Message.sequence), 0)).where(
            Message.conversation_id == conversation_id
        )
        return int((await self._s.execute(stmt)).scalar_one())

    async def streaming(self, conversation_id: uuid.UUID) -> Message | None:
        stmt = select(Message).where(
            Message.conversation_id == conversation_id, Message.status == "streaming"
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def page(
        self,
        workspace_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        before_sequence: int | None,
        limit: int,
        include_superseded: bool,
    ) -> tuple[list[Message], bool]:
        """The ``limit`` messages before ``before_sequence`` (or the latest), oldest first."""
        where: list[Any] = [
            Message.workspace_id == workspace_id,
            Message.conversation_id == conversation_id,
        ]
        if before_sequence is not None:
            where.append(Message.sequence < before_sequence)
        if not include_superseded:
            where.append(Message.superseded_at.is_(None))
        rows = list(
            (
                await self._s.execute(
                    select(Message).where(*where).order_by(Message.sequence.desc()).limit(limit + 1)
                )
            ).scalars()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        rows.reverse()
        return rows, has_more

    async def turns(
        self, conversation_id: uuid.UUID, *, before_sequence: int | None = None, limit: int = 50
    ) -> list[Turn]:
        """Completed exchanges, oldest first: each question with its active completed answer."""
        answer = Message.__table__.alias("answer")
        stmt = (
            select(Message.content, answer.c.content)
            .join(
                answer,
                (answer.c.reply_to_id == Message.id)
                & (answer.c.status == "completed")
                & answer.c.superseded_at.is_(None),
            )
            .where(Message.conversation_id == conversation_id, Message.role == "user")
            .order_by(Message.sequence.desc())
            .limit(limit)
        )
        if before_sequence is not None:
            stmt = stmt.where(Message.sequence < before_sequence)
        rows = (await self._s.execute(stmt)).all()
        return [Turn(q, a) for q, a in reversed(rows)]

    async def citations(
        self, workspace_id: uuid.UUID, message_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[tuple[MessageCitation, bool]]]:
        """Citations per message, each with whether its document is still available."""
        result: dict[uuid.UUID, list[tuple[MessageCitation, bool]]] = {
            mid: [] for mid in message_ids
        }
        if not message_ids:
            return result
        available = (Document.id.is_not(None)) & Document.archived_at.is_(None)
        rows = await self._s.execute(
            select(MessageCitation, available.label("available"))
            .outerjoin(
                Document,
                (Document.id == MessageCitation.document_id)
                & (Document.workspace_id == MessageCitation.workspace_id),
            )
            .where(
                MessageCitation.workspace_id == workspace_id,
                MessageCitation.message_id.in_(message_ids),
            )
            .order_by(MessageCitation.message_id, MessageCitation.number)
        )
        for citation, ok in rows.all():
            result[citation.message_id].append((citation, bool(ok)))
        return result

    async def existing_chunk_ids(self, chunk_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        if not chunk_ids:
            return set()
        rows = await self._s.execute(
            select(DocumentChunk.id).where(DocumentChunk.id.in_(chunk_ids))
        )
        return set(rows.scalars())

    async def fail_stale(
        self, before: datetime, *, conversation_id: uuid.UUID | None = None
    ) -> int:
        """Mark answers that stopped heart-beating as failed (their process died)."""
        stmt = (
            update(Message)
            .where(Message.status == "streaming", Message.updated_at < before)
            .values(status="failed", error_code="interrupted", completed_at=func.now())
            .execution_options(synchronize_session=False)
        )
        if conversation_id is not None:
            stmt = stmt.where(Message.conversation_id == conversation_id)
        result = await self._s.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def touch(self, message_id: uuid.UUID) -> bool:
        result = await self._s.execute(
            update(Message)
            .where(Message.id == message_id, Message.status == "streaming")
            .values(updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return bool(getattr(result, "rowcount", 0))
