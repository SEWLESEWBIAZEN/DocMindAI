"""Extraction runs and tracked deadlines (ADR 0034)."""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity.common import (
    AuditRecorder,
    RequestMeta,
    audit_repository,
    utcnow,
)
from documind.core.config import Settings
from documind.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from documind.domain.extraction.templates import (
    CUSTOM,
    TEMPLATES,
    Field,
    Template,
    TemplateError,
    custom_fields,
)
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext
from documind.infrastructure.database.models import (
    Deadline,
    Document,
    DocumentCollection,
    DocumentCollectionItem,
    ExtractionResult,
    ExtractionRun,
    WorkspaceMember,
)
from documind.infrastructure.database.repositories.visibility import (
    collection_visible,
    document_visible,
    viewer_of,
)

Dispatch = Callable[[uuid.UUID, uuid.UUID], Awaitable[bool]]
DEFAULT_REMINDERS = (7, 1, 0)
_ACTIVE = ("queued", "running")


@dataclass(frozen=True, slots=True)
class RunView:
    run: ExtractionRun
    results: list[ExtractionResult]
    titles: dict[uuid.UUID, str]


def _formula_safe(value: str) -> str:
    """Spreadsheet apps run cells starting with = + - @ as formulas (CSV injection)."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


class ExtractionService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        dispatch: Dispatch,
        meta: RequestMeta | None = None,
    ) -> None:
        self._s = session
        self._settings = settings
        self._dispatch = dispatch
        self._audit = AuditRecorder(audit_repository(session), meta or RequestMeta())

    @staticmethod
    def templates() -> list[Template]:
        return list(TEMPLATES.values())

    async def _documents(
        self,
        ctx: WorkspaceContext,
        document_ids: list[uuid.UUID],
        collection_id: uuid.UUID | None,
    ) -> list[uuid.UUID]:
        wanted = list(dict.fromkeys(document_ids))
        viewer = viewer_of(ctx)
        if collection_id is not None:
            visible = await self._s.execute(
                select(DocumentCollection.id).where(
                    DocumentCollection.workspace_id == ctx.workspace_id,
                    DocumentCollection.id == collection_id,
                    collection_visible(viewer),
                )
            )
            if visible.first() is None:
                raise NotFoundError("Collection not found.")
            rows = await self._s.execute(
                select(DocumentCollectionItem.document_id)
                .join(Document, Document.id == DocumentCollectionItem.document_id)
                .where(
                    DocumentCollectionItem.workspace_id == ctx.workspace_id,
                    DocumentCollectionItem.collection_id == collection_id,
                    Document.archived_at.is_(None),
                    document_visible(Document.id, viewer),
                )
                .order_by(Document.title)
            )
            found = list(rows.scalars())
            if not found and not wanted:
                raise ValidationFailedError(
                    "That collection has no documents.", code="no_documents"
                )
            wanted += [d for d in found if d not in wanted]
        if not wanted:
            raise ValidationFailedError("Choose at least one document.", code="no_documents")
        if len(wanted) > self._settings.extraction_max_documents:
            raise ValidationFailedError(
                f"Choose at most {self._settings.extraction_max_documents} documents per "
                "extraction.",
                code="too_many_documents",
            )
        present = set(
            (
                await self._s.execute(
                    select(Document.id).where(
                        Document.workspace_id == ctx.workspace_id,
                        Document.id.in_(wanted),
                        document_visible(Document.id, viewer),
                    )
                )
            ).scalars()
        )
        if present != set(wanted):
            raise NotFoundError("Document not found.")
        return wanted

    async def start(
        self,
        ctx: WorkspaceContext,
        *,
        template_key: str,
        document_ids: list[uuid.UUID],
        collection_id: uuid.UUID | None = None,
        title: str | None = None,
        fields: list[dict[str, Any]] | None = None,
        mode: str | None = None,
    ) -> ExtractionRun:
        ctx.require(Permission.CONVERSATIONS_USE)
        ctx.require(Permission.DOCUMENTS_READ)
        if template_key == CUSTOM:
            try:
                chosen = custom_fields(fields or [])
            except TemplateError as exc:
                raise ValidationFailedError(str(exc), code="invalid_fields") from exc
            run_mode = mode if mode in ("fields", "items") else "fields"
            name = "Custom extraction"
        elif template_key in TEMPLATES:
            template = TEMPLATES[template_key]
            chosen, run_mode, name = template.fields, template.mode, template.name
        else:
            raise ValidationFailedError(
                f"Unknown template '{template_key}'.", code="invalid_template"
            )
        docs = await self._documents(ctx, document_ids, collection_id)
        active = (
            await self._s.execute(
                select(func.count())
                .select_from(ExtractionRun)
                .where(
                    ExtractionRun.workspace_id == ctx.workspace_id,
                    ExtractionRun.status.in_(_ACTIVE),
                )
            )
        ).scalar_one()
        if active >= self._settings.extraction_active_runs_per_workspace:
            raise ConflictError(
                "Several extractions are already running in this workspace. Try again when one "
                "finishes.",
                code="too_many_extractions",
            )
        run = ExtractionRun(
            id=uuid.uuid4(),
            workspace_id=ctx.workspace_id,
            created_by_user_id=ctx.user_id,
            title=(" ".join((title or "").split())[:200] or name),
            template_key=template_key,
            mode=run_mode,
            fields=[f.to_dict() for f in chosen],
            document_ids=[str(d) for d in docs],
            status="queued",
        )
        self._s.add(run)
        self._audit.record(
            "extraction.started",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="extraction",
            target_id=run.id,
            details={"template": template_key, "documents": len(docs)},
        )
        await self._s.commit()
        await self._dispatch(run.id, ctx.workspace_id)
        return run

    async def runs(self, ctx: WorkspaceContext, *, limit: int = 50) -> list[ExtractionRun]:
        ctx.require(Permission.DOCUMENTS_READ)
        rows = await self._s.execute(
            select(ExtractionRun)
            .where(ExtractionRun.workspace_id == ctx.workspace_id)
            .order_by(ExtractionRun.created_at.desc())
            .limit(limit)
        )
        return list(rows.scalars())

    async def _run(self, ctx: WorkspaceContext, run_id: uuid.UUID) -> ExtractionRun:
        run = await self._s.get(ExtractionRun, run_id)
        if run is None or run.workspace_id != ctx.workspace_id:
            raise NotFoundError("Extraction not found.")
        return run

    async def get(self, ctx: WorkspaceContext, run_id: uuid.UUID) -> RunView:
        ctx.require(Permission.DOCUMENTS_READ)
        run = await self._run(ctx, run_id)
        results = list(
            (
                await self._s.execute(
                    select(ExtractionResult)
                    .where(
                        ExtractionResult.run_id == run.id,
                        ExtractionResult.workspace_id == ctx.workspace_id,
                    )
                    .order_by(ExtractionResult.position)
                )
            ).scalars()
        )
        ids = [uuid.UUID(d) for d in run.document_ids]
        titles = dict(
            (
                await self._s.execute(
                    select(Document.id, Document.title).where(
                        Document.workspace_id == ctx.workspace_id,
                        Document.id.in_(ids),
                        document_visible(Document.id, viewer_of(ctx)),
                    )
                )
            ).all()
        )
        # Someone else's run may cover documents this person can't see (ADR 0036).
        results = [r for r in results if r.document_id in titles]
        return RunView(run, results, titles)

    async def delete(self, ctx: WorkspaceContext, run_id: uuid.UUID) -> None:
        ctx.require(Permission.DOCUMENTS_READ)
        run = await self._run(ctx, run_id)
        if (
            run.created_by_user_id != ctx.user_id
            and Permission.WORKSPACE_UPDATE not in ctx.permissions
        ):
            raise PermissionDeniedError("Only its creator or an admin can delete an extraction.")
        await self._s.execute(delete(ExtractionRun).where(ExtractionRun.id == run.id))
        self._audit.record(
            "extraction.deleted",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="extraction",
            target_id=run.id,
        )
        await self._s.commit()

    async def csv(self, ctx: WorkspaceContext, run_id: uuid.UUID) -> str:
        view = await self.get(ctx, run_id)
        fields = [Field(**f) for f in view.run.fields]
        out = io.StringIO()
        writer = csv.writer(out)
        header = ["Document"]
        for f in fields:
            header += [f.label] + ([f"{f.label} (ISO date)"] if f.type == "date" else [])
        writer.writerow([*header, "Pages"])
        for result in view.results:
            row = [view.titles.get(result.document_id, "(deleted document)")]
            pages: list[str] = []
            for f in fields:
                cell = result.values.get(f.key) or {}
                row.append(cell.get("value") or "")
                if f.type == "date":
                    row.append(cell.get("date") or "")
                if cell.get("page_start"):
                    pages.append(str(cell["page_start"]))
            row.append(", ".join(dict.fromkeys(pages)))
            writer.writerow([_formula_safe(str(v)) for v in row])
        return out.getvalue()


# --------------------------------------------------------------------------- #
# Deadlines
# --------------------------------------------------------------------------- #


def _reminders(raw: list[int] | None) -> list[int]:
    if raw is None:
        return list(DEFAULT_REMINDERS)
    days = sorted({int(d) for d in raw}, reverse=True)
    if any(d < 0 or d > 365 for d in days) or len(days) > 6:
        raise ValidationFailedError(
            "Reminders are 0 to 365 days before the due date, at most 6.", code="invalid_reminders"
        )
    return days


def _clean_title(title: str) -> str:
    title = " ".join(title.split())[:300]
    if not title:
        raise ValidationFailedError("Give the deadline a title.", code="invalid_title")
    return title


class DeadlineService:
    """Deadlines live in the data plane; assignees are checked in the control plane."""

    def __init__(
        self, data: AsyncSession, control: AsyncSession, meta: RequestMeta | None = None
    ) -> None:
        self._s = data
        self._control = control
        self._audit = AuditRecorder(audit_repository(data), meta or RequestMeta())

    async def _assignees(self, ctx: WorkspaceContext, ids: list[uuid.UUID] | None) -> list[str]:
        if not ids:
            return [str(ctx.user_id)]
        wanted = set(ids)
        members = set(
            (
                await self._control.execute(
                    select(WorkspaceMember.user_id).where(
                        WorkspaceMember.workspace_id == ctx.workspace_id,
                        WorkspaceMember.user_id.in_(wanted),
                    )
                )
            ).scalars()
        )
        if members != wanted:
            raise ValidationFailedError(
                "Reminders can only go to members of this workspace.", code="invalid_assignees"
            )
        return [str(u) for u in ids]

    async def _get(self, ctx: WorkspaceContext, deadline_id: uuid.UUID) -> Deadline:
        row = (
            await self._s.execute(
                select(Deadline).where(
                    Deadline.id == deadline_id,
                    Deadline.workspace_id == ctx.workspace_id,
                    or_(
                        Deadline.document_id.is_(None),
                        document_visible(Deadline.document_id, viewer_of(ctx)),
                    ),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("Deadline not found.")
        return row

    async def deadlines(self, ctx: WorkspaceContext, *, status: str = "open") -> list[Deadline]:
        ctx.require(Permission.DOCUMENTS_READ)
        stmt = select(Deadline).where(
            Deadline.workspace_id == ctx.workspace_id,
            or_(
                Deadline.document_id.is_(None),
                document_visible(Deadline.document_id, viewer_of(ctx)),
            ),
        )
        if status in ("open", "done"):
            stmt = stmt.where(Deadline.status == status)
        stmt = stmt.order_by(Deadline.due_date, Deadline.created_at).limit(500)
        return list((await self._s.execute(stmt)).scalars())

    async def create(
        self,
        ctx: WorkspaceContext,
        *,
        title: str,
        due_date: date,
        document_id: uuid.UUID | None = None,
        notes: str | None = None,
        source: dict[str, Any] | None = None,
        assignee_user_ids: list[uuid.UUID] | None = None,
        remind_days_before: list[int] | None = None,
    ) -> Deadline:
        ctx.require(Permission.DOCUMENTS_WRITE)
        if document_id is not None:
            found = await self._s.execute(
                select(Document.id).where(
                    Document.workspace_id == ctx.workspace_id,
                    Document.id == document_id,
                    document_visible(Document.id, viewer_of(ctx)),
                )
            )
            if found.first() is None:
                raise NotFoundError("Document not found.")
        row = Deadline(
            id=uuid.uuid4(),
            workspace_id=ctx.workspace_id,
            document_id=document_id,
            title=_clean_title(title),
            due_date=due_date,
            notes=(notes or "").strip()[:2000] or None,
            source=_clean_source(source),
            assignee_user_ids=await self._assignees(ctx, assignee_user_ids),
            remind_days_before=_reminders(remind_days_before),
            reminders_sent=[],
            status="open",
            created_by_user_id=ctx.user_id,
        )
        self._s.add(row)
        self._audit.record(
            "deadline.created",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="deadline",
            target_id=row.id,
            details={"due_date": due_date.isoformat()},
        )
        await self._s.commit()
        return row

    async def update(
        self, ctx: WorkspaceContext, deadline_id: uuid.UUID, changes: dict[str, Any]
    ) -> Deadline:
        ctx.require(Permission.DOCUMENTS_WRITE)
        row = await self._get(ctx, deadline_id)
        if "title" in changes:
            row.title = _clean_title(changes["title"])
        if "due_date" in changes and changes["due_date"] != row.due_date:
            row.due_date = changes["due_date"]
            row.reminders_sent = []  # new date, new reminders
        if "notes" in changes:
            row.notes = (changes["notes"] or "").strip()[:2000] or None
        if "assignee_user_ids" in changes:
            row.assignee_user_ids = await self._assignees(ctx, changes["assignee_user_ids"])
        if "remind_days_before" in changes:
            row.remind_days_before = _reminders(changes["remind_days_before"])
            row.reminders_sent = [d for d in row.reminders_sent if d in row.remind_days_before]
        if "status" in changes and changes["status"] != row.status:
            if changes["status"] not in ("open", "done"):
                raise ValidationFailedError("Status is open or done.", code="invalid_status")
            row.status = changes["status"]
            row.completed_at = utcnow() if row.status == "done" else None
        self._audit.record(
            "deadline.updated",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="deadline",
            target_id=row.id,
            details={"fields": sorted(changes)},
        )
        await self._s.commit()
        return row

    async def delete(self, ctx: WorkspaceContext, deadline_id: uuid.UUID) -> None:
        ctx.require(Permission.DOCUMENTS_WRITE)
        row = await self._get(ctx, deadline_id)
        await self._s.delete(row)
        self._audit.record(
            "deadline.deleted",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="deadline",
            target_id=deadline_id,
        )
        await self._s.commit()

    async def titles(self, ctx: WorkspaceContext, rows: list[Deadline]) -> dict[uuid.UUID, str]:
        ids = {r.document_id for r in rows if r.document_id}
        if not ids:
            return {}
        return dict(
            (
                await self._s.execute(
                    select(Document.id, Document.title).where(
                        Document.workspace_id == ctx.workspace_id, Document.id.in_(ids)
                    )
                )
            ).all()
        )

    async def calendar(self, ctx: WorkspaceContext, base_url: str) -> str:
        rows = await self.deadlines(ctx, status="open")
        titles = await self.titles(ctx, rows)
        return ics(rows, titles, base_url, ctx.workspace_id)


def _clean_source(source: dict[str, Any] | None) -> dict[str, Any] | None:
    if not source:
        return None
    keep = {"chunk_id", "page_start", "page_end", "excerpt"}
    out = {k: v for k, v in source.items() if k in keep}
    if isinstance(out.get("excerpt"), str):
        out["excerpt"] = out["excerpt"][:600]
    return out or None


def _ics_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", r"\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: lines over 75 octets continue on the next line after a space."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts: list[str] = []
    current = b""
    for ch in line:
        b = ch.encode()
        if len(current) + len(b) > (75 if not parts else 74):
            parts.append(current.decode())
            current = b""
        current += b
    parts.append(current.decode())
    return "\r\n ".join(parts)


def ics(
    rows: list[Deadline], titles: dict[uuid.UUID, str], base_url: str, workspace_id: uuid.UUID
) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//DocuMind//Deadlines//EN",
        "CALSCALE:GREGORIAN",
    ]
    for row in rows:
        end = date.fromordinal(row.due_date.toordinal() + 1)
        description = []
        if row.document_id and row.document_id in titles:
            description.append(f"Document: {titles[row.document_id]}")
            description.append(
                f"{base_url.rstrip('/')}/w/{workspace_id}/documents/{row.document_id}"
            )
        if row.notes:
            description.append(row.notes)
        lines += [
            "BEGIN:VEVENT",
            f"UID:deadline-{row.id}@documind",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{row.due_date:%Y%m%d}",
            f"DTEND;VALUE=DATE:{end:%Y%m%d}",
            f"SUMMARY:{_ics_text(row.title)}",
        ]
        if description:
            lines.append(f"DESCRIPTION:{_ics_text(chr(10).join(description))}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
