"""Workspace usage summary, computed from operational data.

A read-only aggregate for the usage dashboard. Phase 8 introduces persisted
usage records, quotas and plans; this summary will then read from them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Date, Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity.common import utcnow
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext
from documind.infrastructure.database.models import (
    Conversation,
    Document,
    DocumentVersion,
    Message,
    ProcessingJob,
    User,
)


@dataclass(frozen=True, slots=True)
class DocumentUsage:
    total: int
    ready: int
    processing: int
    failed: int
    archived: int
    storage_bytes: int
    pages: int
    chunks: int


@dataclass(frozen=True, slots=True)
class ProcessingUsage:
    jobs_succeeded: int
    jobs_failed: int
    embedding_tokens: int


@dataclass(frozen=True, slots=True)
class AiUsage:
    conversations: int
    questions: int
    answers_completed: int
    answers_failed: int
    answers_cancelled: int
    llm_input_tokens: int
    llm_output_tokens: int
    query_embedding_tokens: int


@dataclass(frozen=True, slots=True)
class DailyUsage:
    day: date
    questions: int
    llm_input_tokens: int
    llm_output_tokens: int
    documents_processed: int


@dataclass(frozen=True, slots=True)
class MemberUsage:
    user_id: uuid.UUID
    display_name: str
    questions: int
    llm_tokens: int


@dataclass(frozen=True, slots=True)
class UsageSummary:
    period_start: datetime
    period_end: datetime
    documents: DocumentUsage
    processing: ProcessingUsage
    ai: AiUsage
    daily: list[DailyUsage]
    members: list[MemberUsage]


def _i(value: object) -> int:
    return int(str(value)) if value is not None else 0


class UsageQueries:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def summary(self, ctx: WorkspaceContext, *, days: int) -> UsageSummary:
        ctx.require(Permission.USAGE_READ)
        ws = ctx.workspace_id
        end = utcnow()
        start = (end - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

        d = (
            await self._s.execute(
                select(
                    func.count(),
                    func.count().filter(Document.status == "ready"),
                    func.count().filter(Document.status.in_(("pending", "processing"))),
                    func.count().filter(Document.status == "failed"),
                    func.count().filter(Document.archived_at.is_not(None)),
                    func.coalesce(func.sum(Document.page_count), 0),
                    func.coalesce(func.sum(Document.chunk_count), 0),
                ).where(Document.workspace_id == ws)
            )
        ).one()
        storage = (
            await self._s.execute(
                select(func.coalesce(func.sum(DocumentVersion.size_bytes), 0)).where(
                    DocumentVersion.workspace_id == ws
                )
            )
        ).scalar_one()

        tokens = cast(ProcessingJob.stats["embedding_input_tokens"].astext, Integer)
        p = (
            await self._s.execute(
                select(
                    func.count().filter(ProcessingJob.status == "succeeded"),
                    func.count().filter(ProcessingJob.status.in_(("failed", "dead"))),
                    func.coalesce(func.sum(tokens).filter(ProcessingJob.status == "succeeded"), 0),
                ).where(ProcessingJob.workspace_id == ws, ProcessingJob.created_at >= start)
            )
        ).one()

        in_period = (Message.workspace_id == ws, Message.created_at >= start)
        a = (
            await self._s.execute(
                select(
                    func.count().filter(Message.role == "user"),
                    func.count().filter(Message.role == "assistant", Message.status == "completed"),
                    func.count().filter(Message.role == "assistant", Message.status == "failed"),
                    func.count().filter(Message.role == "assistant", Message.status == "cancelled"),
                    func.coalesce(func.sum(Message.input_tokens), 0),
                    func.coalesce(func.sum(Message.output_tokens), 0),
                    func.coalesce(func.sum(Message.embedding_tokens), 0),
                ).where(*in_period)
            )
        ).one()
        conversations = (
            await self._s.execute(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.workspace_id == ws)
            )
        ).scalar_one()

        day = cast(func.date_trunc("day", Message.created_at), Date).label("day")
        msg_rows = (
            await self._s.execute(
                select(
                    day,
                    func.count().filter(Message.role == "user"),
                    func.coalesce(func.sum(Message.input_tokens), 0),
                    func.coalesce(func.sum(Message.output_tokens), 0),
                )
                .where(*in_period)
                .group_by(day)
            )
        ).all()
        job_day = cast(func.date_trunc("day", ProcessingJob.finished_at), Date).label("day")
        job_rows = (
            await self._s.execute(
                select(job_day, func.count())
                .where(
                    ProcessingJob.workspace_id == ws,
                    ProcessingJob.status == "succeeded",
                    ProcessingJob.finished_at >= start,
                )
                .group_by(job_day)
            )
        ).all()
        by_day: dict[date, list[int]] = {}
        for row in msg_rows:
            by_day[row[0]] = [_i(row[1]), _i(row[2]), _i(row[3]), 0]
        for job_day_value, processed in job_rows:
            by_day.setdefault(job_day_value, [0, 0, 0, 0])[3] = _i(processed)
        daily = []
        for offset in range(days):
            current = (start + timedelta(days=offset)).date()
            q, i, o, docs = by_day.get(current, [0, 0, 0, 0])
            daily.append(DailyUsage(current, q, i, o, docs))

        member_rows = (
            await self._s.execute(
                select(
                    User.id,
                    User.display_name,
                    func.count().filter(Message.role == "user"),
                    func.coalesce(func.sum(Message.input_tokens + Message.output_tokens), 0),
                )
                .join(Conversation, Conversation.id == Message.conversation_id)
                .join(User, User.id == Conversation.created_by_user_id)
                .where(*in_period, Conversation.workspace_id == ws)
                .group_by(User.id, User.display_name)
                .order_by(func.count().filter(Message.role == "user").desc(), User.display_name)
                .limit(20)
            )
        ).all()

        return UsageSummary(
            period_start=start,
            period_end=end,
            documents=DocumentUsage(
                total=_i(d[0]),
                ready=_i(d[1]),
                processing=_i(d[2]),
                failed=_i(d[3]),
                archived=_i(d[4]),
                storage_bytes=_i(storage),
                pages=_i(d[5]),
                chunks=_i(d[6]),
            ),
            processing=ProcessingUsage(_i(p[0]), _i(p[1]), _i(p[2])),
            ai=AiUsage(
                conversations=_i(conversations),
                questions=_i(a[0]),
                answers_completed=_i(a[1]),
                answers_failed=_i(a[2]),
                answers_cancelled=_i(a[3]),
                llm_input_tokens=_i(a[4]),
                llm_output_tokens=_i(a[5]),
                query_embedding_tokens=_i(a[6]),
            ),
            daily=daily,
            members=[MemberUsage(r[0], r[1], _i(r[2]), _i(r[3])) for r in member_rows],
        )
