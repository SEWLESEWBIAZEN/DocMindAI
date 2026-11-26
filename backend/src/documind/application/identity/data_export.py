""" "Download my data" (ADR 0039).

Asking for an export queues a background job. The job writes one ZIP to the
private application bucket and emails the person when it's ready. It downloads
through a short-lived signed link, only for the person it belongs to, for a few days.

What's in it:

* ``account.json``: profile, sign-in methods (never secrets), workspaces and roles;
* per workspace: the person's own conversations (questions, answers, sources and
  their ratings) and details of the documents they uploaded;
* their personal workspace's original files, up to ``DATA_EXPORT_MAX_FILE_BYTES``.

Organizations' files belong to the organization and stay in the app; what this
person uploaded there is listed, not copied.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import IO, Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.identity import emails
from documind.application.identity.common import utcnow
from documind.application.infrastructure.resolver import InfrastructureResolver
from documind.application.ports import EmailSender, ObjectStorageProvider, RateLimiter
from documind.core.config import Settings
from documind.domain.errors import NotFoundError
from documind.infrastructure.database.models import (
    Conversation,
    DataExport,
    Document,
    DocumentVersion,
    Message,
    MessageCitation,
    MessageFeedback,
    Role,
    User,
    WebAuthnCredential,
    Workspace,
    WorkspaceMember,
)

logger = logging.getLogger(__name__)

ACTIVE = ("pending", "running")
#: A job that hasn't finished in this long is reported as failed by the sweep.
STALE_AFTER = timedelta(hours=2)
DOWNLOAD_LINK_SECONDS = 300
MAX_PER_DAY = 3

README = """DocuMind data export
====================

account.json            Your profile, sign-in methods and workspaces.
workspaces/<name>/
  conversations.json    Your conversations: questions, answers, sources and your ratings.
  documents.json        Documents you uploaded to this workspace.
  files/                Original files (your personal workspace only).

Documents you uploaded to an organization belong to that organization; they are
listed here and can be downloaded in the app while you're a member.
"""


@dataclass(frozen=True, slots=True)
class ExportInfo:
    id: uuid.UUID
    status: str
    size_bytes: int | None
    created_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None
    error: str | None


def _info(row: DataExport) -> ExportInfo:
    return ExportInfo(
        id=row.id,
        status=row.status,
        size_bytes=row.size_bytes,
        created_at=row.created_at,
        completed_at=row.completed_at,
        expires_at=row.expires_at,
        error=row.error,
    )


def _object_key(user_id: uuid.UUID, export_id: uuid.UUID) -> str:
    return f"exports/{user_id}/{export_id}.zip"


class DataExportService:
    def __init__(
        self,
        session: AsyncSession,
        limiter: RateLimiter,
        storage: ObjectStorageProvider,
        dispatch: Callable[[uuid.UUID], Awaitable[bool]] | None,
    ) -> None:
        self._s = session
        self._limiter = limiter
        self._storage = storage
        self._dispatch = dispatch

    async def request(self, user: User) -> ExportInfo:
        """Start an export, or return the one already being prepared."""
        running = (
            await self._s.execute(
                select(DataExport).where(
                    DataExport.user_id == user.id, DataExport.status.in_(ACTIVE)
                )
            )
        ).scalar_one_or_none()
        if running is not None:
            return _info(running)
        await self._limiter.hit("data-export:user", str(user.id), MAX_PER_DAY, 24 * 3600)
        row = DataExport(user_id=user.id)
        self._s.add(row)
        await self._s.commit()
        await self._s.refresh(row)
        queued = self._dispatch is not None and await self._dispatch(row.id)
        if not queued:
            row.status, row.error = "failed", "Couldn't start the export. Try again later."
            await self._s.commit()
        return _info(row)

    async def list(self, user: User) -> list[ExportInfo]:
        rows = await self._s.execute(
            select(DataExport)
            .where(DataExport.user_id == user.id)
            .order_by(DataExport.created_at.desc())
            .limit(5)
        )
        return [_info(r) for r in rows.scalars()]

    async def download_url(self, user: User, export_id: uuid.UUID) -> str:
        row = (
            await self._s.execute(
                select(DataExport).where(DataExport.id == export_id, DataExport.user_id == user.id)
            )
        ).scalar_one_or_none()
        now = utcnow()
        if (
            row is None
            or row.status != "ready"
            or row.object_key is None
            or (row.expires_at is not None and row.expires_at <= now)
        ):
            raise NotFoundError("This export isn't available. Ask for a new one.")
        return self._storage.generate_signed_url(
            row.object_key,
            expires_in=DOWNLOAD_LINK_SECONDS,
            download_filename=f"documind-export-{row.created_at:%Y-%m-%d}.zip",
            content_type="application/zip",
        )


# --------------------------------------------------------------------------- #
# The background job
# --------------------------------------------------------------------------- #


def _safe(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^\w .()-]+", "_", name).strip(" ._")[:80]
    return cleaned or fallback


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class _Zip:
    """A ZIP on disk (exports can be large), with unique names."""

    def __init__(self, file: IO[bytes]) -> None:
        self._zip = zipfile.ZipFile(file, "w", compression=zipfile.ZIP_DEFLATED)
        self._names: set[str] = set()

    def name(self, path: str) -> str:
        base, dot, ext = path.rpartition(".")
        candidate, n = path, 2
        while candidate in self._names:
            candidate = f"{base} ({n}).{ext}" if dot else f"{path} ({n})"
            n += 1
        self._names.add(candidate)
        return candidate

    def json(self, path: str, data: Any) -> None:
        self._zip.writestr(self.name(path), json.dumps(data, indent=2, ensure_ascii=False))

    def text(self, path: str, text: str) -> None:
        self._zip.writestr(self.name(path), text)

    def file(self, path: str, source: IO[bytes]) -> None:
        with self._zip.open(self.name(path), "w") as out:
            while chunk := source.read(1024 * 1024):
                out.write(chunk)

    def close(self) -> None:
        self._zip.close()


async def _conversations(
    data: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> list[dict[str, Any]]:
    convs = list(
        (
            await data.execute(
                select(Conversation)
                .where(
                    Conversation.workspace_id == workspace_id,
                    Conversation.created_by_user_id == user_id,
                )
                .order_by(Conversation.created_at)
            )
        ).scalars()
    )
    if not convs:
        return []
    ids = [c.id for c in convs]
    messages = list(
        (
            await data.execute(
                select(Message)
                .where(Message.workspace_id == workspace_id, Message.conversation_id.in_(ids))
                .order_by(Message.conversation_id, Message.sequence)
            )
        ).scalars()
    )
    mids = [m.id for m in messages]
    citations: dict[uuid.UUID, list[dict[str, Any]]] = {}
    feedback: dict[uuid.UUID, MessageFeedback] = {}
    if mids:
        for c in (
            await data.execute(
                select(MessageCitation)
                .where(
                    MessageCitation.workspace_id == workspace_id,
                    MessageCitation.message_id.in_(mids),
                )
                .order_by(MessageCitation.number)
            )
        ).scalars():
            citations.setdefault(c.message_id, []).append(
                {"number": c.number, "source": c.label, "excerpt": c.excerpt}
            )
        for row in (
            await data.execute(
                select(MessageFeedback).where(
                    MessageFeedback.workspace_id == workspace_id,
                    MessageFeedback.message_id.in_(mids),
                )
            )
        ).scalars():
            feedback[row.message_id] = row
    by_conv: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for m in messages:
        f = feedback.get(m.id)
        by_conv.setdefault(m.conversation_id, []).append(
            {
                "role": m.role,
                "text": m.content,
                "status": m.status,
                "created_at": _iso(m.created_at),
                "sources": citations.get(m.id, []),
                **(
                    {
                        "your_rating": {
                            "rating": f.rating,
                            "reasons": list(f.reasons or []),
                            "comment": f.comment,
                            "shared_with_admins": f.shared,
                        }
                    }
                    if f
                    else {}
                ),
            }
        )
    return [
        {
            "title": c.title,
            "created_at": _iso(c.created_at),
            "messages": by_conv.get(c.id, []),
        }
        for c in convs
    ]


async def build_export(
    export_id: uuid.UUID,
    *,
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    infrastructure: InfrastructureResolver,
    storage: ObjectStorageProvider,
    email: EmailSender,
) -> str:
    """Build one export. Returns an outcome label."""
    async with sessions() as session:
        claimed = await session.execute(
            update(DataExport)
            .where(DataExport.id == export_id, DataExport.status == "pending")
            .values(status="running")
            .returning(DataExport.user_id)
        )
        user_id = claimed.scalar_one_or_none()
        await session.commit()
        if user_id is None:
            return "skipped"
        user = await session.get(User, user_id)
        if user is None:
            return "skipped"
        try:
            size = await _write(session, user, export_id, settings, infrastructure, storage)
        except Exception:
            logger.exception("data export failed", extra={"export_id": str(export_id)})
            await session.rollback()
            await session.execute(
                update(DataExport)
                .where(DataExport.id == export_id)
                .values(status="failed", error="Something went wrong. Try again later.")
            )
            await session.commit()
            return "failed"
        now = utcnow()
        await session.execute(
            update(DataExport)
            .where(DataExport.id == export_id)
            .values(
                status="ready",
                object_key=_object_key(user.id, export_id),
                size_bytes=size,
                completed_at=now,
                expires_at=now + timedelta(days=settings.data_export_ttl_days),
            )
        )
        await session.commit()
        await email.send(
            emails.data_export_ready_email(
                user.email,
                user.display_name,
                settings.public_app_url,
                settings.data_export_ttl_days,
            )
        )
        return "ready"


async def _write(
    session: AsyncSession,
    user: User,
    export_id: uuid.UUID,
    settings: Settings,
    infrastructure: InfrastructureResolver,
    storage: ObjectStorageProvider,
) -> int:
    memberships = (
        await session.execute(
            select(Workspace, Role.key, WorkspaceMember.created_at)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .join(Role, Role.id == WorkspaceMember.role_id)
            .where(WorkspaceMember.user_id == user.id)
            .order_by(Workspace.kind.desc(), Workspace.name)
        )
    ).all()
    passkeys = (
        await session.execute(
            select(WebAuthnCredential).where(WebAuthnCredential.user_id == user.id)
        )
    ).scalars()
    with tempfile.TemporaryFile() as tmp:
        archive = _Zip(tmp)
        archive.text("README.txt", README)
        archive.json(
            "account.json",
            {
                "exported_at": _iso(utcnow()),
                "id": str(user.id),
                "email": user.email,
                "display_name": user.display_name,
                "created_at": _iso(user.created_at),
                "email_verified_at": _iso(user.email_verified_at),
                "last_login_at": _iso(user.last_login_at),
                "two_factor": {
                    "authenticator_app": user.totp_enabled_at is not None,
                    "email_codes": user.mfa_email_enabled_at is not None,
                    "passkeys": [
                        {"name": p.name, "created_at": _iso(p.created_at)} for p in passkeys
                    ],
                },
                "workspaces": [
                    {"name": w.name, "kind": w.kind, "role": str(role), "joined_at": _iso(joined)}
                    for w, role, joined in memberships
                ],
            },
        )
        budget = settings.data_export_max_file_bytes
        skipped_files = 0
        for w, _, _ in memberships:
            folder = f"workspaces/{_safe(w.name, str(w.id))}"
            infra = await infrastructure.resolve(w.id)
            async with infra.data_plane.sessions() as data:
                archive.json(
                    f"{folder}/conversations.json", await _conversations(data, w.id, user.id)
                )
                docs = list(
                    (
                        await data.execute(
                            select(Document, DocumentVersion)
                            .join(
                                DocumentVersion,
                                (DocumentVersion.id == Document.current_version_id)
                                & (DocumentVersion.workspace_id == Document.workspace_id),
                            )
                            .where(
                                Document.workspace_id == w.id,
                                Document.uploaded_by_user_id == user.id,
                            )
                            .order_by(Document.created_at)
                        )
                    ).all()
                )
            archive.json(
                f"{folder}/documents.json",
                [
                    {
                        "title": d.title,
                        "file_name": d.original_filename,
                        "format": d.format,
                        "size_bytes": d.size_bytes,
                        "versions": d.version_count,
                        "uploaded_at": _iso(d.created_at),
                        "archived": d.archived_at is not None,
                    }
                    for d, _ in docs
                ],
            )
            if w.kind != "personal":
                continue
            for d, v in docs:
                if v.size_bytes > budget:
                    skipped_files += 1
                    continue
                budget -= v.size_bytes
                with tempfile.TemporaryFile() as blob:
                    await infra.storage.download_to(v.storage_key, blob)
                    blob.seek(0)
                    archive.file(f"{folder}/files/{_safe(v.original_filename, str(d.id))}", blob)
        if skipped_files:
            archive.text(
                "FILES_NOT_INCLUDED.txt",
                f"{skipped_files} file(s) didn't fit in this export. Download them from your "
                "personal workspace in the app.\n",
            )
        archive.close()
        size = tmp.tell()
        tmp.seek(0)
        await storage.upload(_object_key(user.id, export_id), tmp, content_type="application/zip")
        return size


async def sweep_exports(
    sessions: async_sessionmaker[AsyncSession], storage: ObjectStorageProvider
) -> int:
    """Delete expired archives; report exports that never finished as failed."""
    now = utcnow()
    changed = 0
    async with sessions() as session:
        expired = list(
            (
                await session.execute(
                    select(DataExport).where(
                        DataExport.status == "ready", DataExport.expires_at <= now
                    )
                )
            ).scalars()
        )
        for row in expired:
            if row.object_key:
                try:
                    await storage.delete(row.object_key)
                except Exception:
                    logger.exception("could not delete an expired export")
                    continue
            row.status, row.object_key = "expired", None
            changed += 1
        stuck = await session.execute(
            update(DataExport)
            .where(DataExport.status.in_(ACTIVE), DataExport.created_at <= now - STALE_AFTER)
            .values(status="failed", error="The export didn't finish. Try again.")
        )
        changed += stuck.rowcount or 0  # type: ignore[attr-defined]
        await session.commit()
    return changed
