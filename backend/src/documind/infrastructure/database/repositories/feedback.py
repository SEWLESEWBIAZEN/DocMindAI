"""Answer feedback (ADR 0038): one rating per answer, and the admin summary queries."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Integer, case, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from documind.infrastructure.database.models import Message, MessageCitation, MessageFeedback


class FeedbackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def for_messages(
        self, workspace_id: uuid.UUID, message_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, MessageFeedback]:
        if not message_ids:
            return {}
        rows = await self._s.execute(
            select(MessageFeedback).where(
                MessageFeedback.workspace_id == workspace_id,
                MessageFeedback.message_id.in_(message_ids),
            )
        )
        return {f.message_id: f for f in rows.scalars()}

    async def upsert(self, values: dict[str, Any]) -> MessageFeedback:
        stmt = insert(MessageFeedback).values(**values)
        changes = {k: stmt.excluded[k] for k in ("rating", "reasons", "comment", "shared")}
        upsert = stmt.on_conflict_do_update(
            index_elements=[MessageFeedback.message_id],
            set_={**changes, "updated_at": func.now()},
        ).returning(MessageFeedback)
        row: MessageFeedback = (await self._s.execute(upsert)).scalar_one()
        await self._s.refresh(row)
        return row

    async def delete(self, workspace_id: uuid.UUID, message_id: uuid.UUID) -> None:
        await self._s.execute(
            delete(MessageFeedback).where(
                MessageFeedback.workspace_id == workspace_id,
                MessageFeedback.message_id == message_id,
            )
        )

    async def answer_counts(self, workspace_id: uuid.UUID, since: datetime) -> tuple[int, int]:
        """Completed answers since ``since``, and how many said the documents didn't cover it."""
        insufficient = func.sum(
            case((Message.answer_status == "insufficient_context", 1), else_=0)
        ).cast(Integer)
        row = (
            await self._s.execute(
                select(func.count(), func.coalesce(insufficient, 0)).where(
                    Message.workspace_id == workspace_id,
                    Message.role == "assistant",
                    Message.status == "completed",
                    Message.created_at >= since,
                )
            )
        ).one()
        return int(row[0]), int(row[1])

    async def ratings(
        self, workspace_id: uuid.UUID, since: datetime
    ) -> list[tuple[str, list[str], str | None]]:
        """(rating, reasons, model) of every rating since ``since``."""
        rows = await self._s.execute(
            select(MessageFeedback.rating, MessageFeedback.reasons, Message.model)
            .join(
                Message,
                (Message.id == MessageFeedback.message_id)
                & (Message.workspace_id == MessageFeedback.workspace_id),
            )
            .where(
                MessageFeedback.workspace_id == workspace_id, MessageFeedback.updated_at >= since
            )
        )
        return [(r, list(reasons or []), model) for r, reasons, model in rows.all()]

    async def shared(
        self, workspace_id: uuid.UUID, since: datetime, *, limit: int
    ) -> list[tuple[MessageFeedback, Message, str, int]]:
        """Ratings members chose to share: (feedback, answer, question text, citation count)."""
        question = Message.__table__.alias("question")
        cited = (
            select(func.count())
            .where(
                MessageCitation.message_id == Message.id,
                MessageCitation.workspace_id == Message.workspace_id,
            )
            .correlate(Message)
            .scalar_subquery()
        )
        rows = await self._s.execute(
            select(MessageFeedback, Message, question.c.content, cited)
            .join(
                Message,
                (Message.id == MessageFeedback.message_id)
                & (Message.workspace_id == MessageFeedback.workspace_id),
            )
            .outerjoin(question, question.c.id == Message.reply_to_id)
            .where(
                MessageFeedback.workspace_id == workspace_id,
                MessageFeedback.shared.is_(True),
                MessageFeedback.updated_at >= since,
            )
            .order_by(MessageFeedback.updated_at.desc())
            .limit(limit)
        )
        return [(f, m, q or "", int(n)) for f, m, q, n in rows.all()]
