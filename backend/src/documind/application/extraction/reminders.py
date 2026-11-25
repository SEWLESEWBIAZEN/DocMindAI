"""Deadline reminder emails and recovery of stuck extraction runs (ADR 0034)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from html import escape

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.identity.common import utcnow
from documind.application.ports import EmailMessage, EmailSender
from documind.infrastructure.database.models import (
    Deadline,
    Document,
    ExtractionRun,
    User,
    WorkspaceMember,
)

logger = logging.getLogger(__name__)


def _when(days: int) -> str:
    if days <= 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def reminder_email(
    to: str, name: str, row: Deadline, document: str | None, base_url: str, days: int
) -> EmailMessage:
    base = base_url.rstrip("/")
    deadlines_url = f"{base}/w/{row.workspace_id}/deadlines"
    doc_url = (
        f"{base}/w/{row.workspace_id}/documents/{row.document_id}" if row.document_id else None
    )
    when = _when(days)
    lines = [f"Hi {name},", f"“{row.title}” is due {when} ({row.due_date:%d %B %Y})."]
    if document:
        lines.append(f"Document: {document}")
    if row.notes:
        lines.append(row.notes)
    text = "\n\n".join(lines) + (f"\n\nOpen the document: {doc_url}" if doc_url else "")
    text += f"\n\nAll deadlines: {deadlines_url}"
    html = (
        '<div style="font-family:system-ui,sans-serif;max-width:560px">'
        f'<h1 style="font-size:20px">Due {escape(when)}: {escape(row.title)}</h1>'
        + "".join(f"<p>{escape(line)}</p>" for line in lines[1:])
        + (f'<p><a href="{escape(doc_url)}">Open the document</a></p>' if doc_url else "")
        + f'<p><a href="{escape(deadlines_url)}">All deadlines</a></p></div>'
    )
    return EmailMessage(to=to, subject=f"Due {when}: {row.title}"[:200], text=text, html=html)


async def send_reminders(
    data: async_sessionmaker[AsyncSession],
    control: async_sessionmaker[AsyncSession],
    email: EmailSender,
    base_url: str,
    *,
    today: date | None = None,
) -> int:
    """Send each due reminder once. Missed reminders (worker down) are caught up once."""
    today = today or utcnow().date()
    sent = 0
    async with data() as s:
        rows = list(
            (
                await s.execute(
                    select(Deadline)
                    .where(
                        Deadline.status == "open",
                        Deadline.due_date >= today,
                        Deadline.due_date <= today + timedelta(days=366),
                    )
                    .order_by(Deadline.due_date)
                    .limit(2000)
                )
            ).scalars()
        )
        for row in rows:
            days = (row.due_date - today).days
            pending = [
                d for d in row.remind_days_before if d >= days and d not in row.reminders_sent
            ]
            if not pending:
                continue
            title = None
            if row.document_id:
                doc = await s.get(Document, row.document_id)
                title = doc.title if doc else None
            async with control() as c:
                ids = [uuid.UUID(u) for u in row.assignee_user_ids]
                people = (
                    await c.execute(
                        select(User.email, User.display_name)
                        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
                        .where(
                            WorkspaceMember.workspace_id == row.workspace_id,
                            User.id.in_(ids),
                            User.is_active.is_(True),
                        )
                    )
                ).all()
            for address, name in people:
                try:
                    await email.send(reminder_email(address, name, row, title, base_url, days))
                    sent += 1
                except Exception:
                    logger.warning("deadline reminder not sent", exc_info=True)
            row.reminders_sent = sorted(set(row.reminders_sent) | set(pending), reverse=True)
            await s.commit()
    return sent


async def recover_extractions(
    data: async_sessionmaker[AsyncSession],
    dispatch: Callable[[uuid.UUID, uuid.UUID], Awaitable[bool]],
    *,
    stale_seconds: int,
) -> int:
    """Fail runs whose worker stopped; re-send runs whose message was lost."""
    now = utcnow()
    changed = 0
    async with data() as s:
        stuck = (
            await s.execute(
                select(ExtractionRun).where(
                    ExtractionRun.status == "running",
                    ExtractionRun.heartbeat_at < now - timedelta(seconds=stale_seconds),
                )
            )
        ).scalars()
        for run in list(stuck):
            run.status, run.error_code, run.completed_at = "failed", "interrupted", now
            changed += 1
        lost = list(
            (
                await s.execute(
                    select(ExtractionRun).where(
                        ExtractionRun.status == "queued",
                        ExtractionRun.created_at < now - timedelta(minutes=2),
                    )
                )
            ).scalars()
        )
        await s.commit()
    for run in lost:
        if await dispatch(run.id, run.workspace_id):
            changed += 1
    return changed
