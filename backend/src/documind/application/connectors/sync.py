"""Keeps a connection's folders in sync with the workspace (ADR 0035).

First sync: walk the chosen folders (and their subfolders), import supported
files, then remember Drive's change cursor. Later syncs read only the changes.

* new or changed file in a synced folder → upload, or a new version;
* removed, trashed or moved out → the document is archived (never deleted);
* back again → restored.

Imports run as the person who connected the drive and go through the normal
upload pipeline (scanning, processing, plan quotas, audit). A sync that hits a
quota pauses the connection; the next one continues where it stopped.
"""

from __future__ import annotations

import hashlib
import io
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.billing.service import BillingService
from documind.application.connectors.ports import (
    SUPPORTED,
    ConnectorAuthError,
    ConnectorError,
    DriveClient,
    DriveFile,
)
from documind.application.credentials.vault import CredentialVault
from documind.application.documents.document_service import DocumentService
from documind.application.identity.common import utcnow
from documind.domain.errors import ConflictError, DomainError, QuotaExceededError
from documind.domain.identity.permissions import Permission, RoleKey
from documind.domain.identity.policies import WorkspaceContext, WorkspaceKind
from documind.infrastructure.database.models import (
    ConnectorConnection,
    ConnectorItem,
    Document,
    Role,
    Workspace,
    WorkspaceMember,
)
from documind.infrastructure.security.vault import EnvelopeCipher

logger = logging.getLogger(__name__)

#: What the connector may do in the workspace, on behalf of the person who connected it.
_SYNC_PERMISSIONS = frozenset(
    {Permission.DOCUMENTS_READ, Permission.DOCUMENTS_WRITE, Permission.DOCUMENTS_ARCHIVE_ANY}
)
_MAX_DEPTH = 20


@dataclass
class SyncReport:
    imported: int = 0
    updated: int = 0
    archived: int = 0
    skipped: int = 0
    status: str = "ok"
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DataAccess:
    """Per-workspace data plane: a session factory and a document service for a session."""

    sessions: async_sessionmaker[AsyncSession]
    documents: Callable[[AsyncSession], DocumentService]


def _filename(file: DriveFile) -> str:
    ext = SUPPORTED[file.mime_type]
    name = file.name.strip() or "Untitled"
    return name if name.lower().endswith(ext) else f"{name}{ext}"


def _title(file: DriveFile) -> str:
    name = file.name.strip() or "Untitled"
    ext = SUPPORTED.get(file.mime_type, "")
    return name[: -len(ext)] if ext and name.lower().endswith(ext) else name


class DriveSync:
    def __init__(
        self,
        control: async_sessionmaker[AsyncSession],
        drive: DriveClient,
        cipher: EnvelopeCipher | None,
        data_for: Callable[[uuid.UUID], Awaitable[DataAccess]],
        *,
        max_files: int,
        max_bytes: int,
    ) -> None:
        self._control = control
        self._drive = drive
        self._cipher = cipher
        self._data_for = data_for
        self._max_files = max_files
        self._max_bytes = max_bytes

    async def run(self, connection_id: uuid.UUID) -> SyncReport:
        report = SyncReport()
        async with self._control() as c:
            row = await c.get(ConnectorConnection, connection_id)
            if row is None or row.status not in ("active", "paused"):
                report.status = "skipped"
                return report
            ctx = await self._context(c, row)
            if ctx is None:
                row.status, row.last_error = "error", "owner_left"
                await c.commit()
                report.status = "owner_left"
                return report
            vault = CredentialVault(c, self._cipher)
            cred = (
                await vault.get(row.workspace_id, row.credential_id) if row.credential_id else None
            )
            try:
                if cred is None:
                    raise ConnectorAuthError("no credential")
                token = await self._drive.access_token(vault.reveal(cred))
                data = await self._data_for(row.workspace_id)
                await self._sync(c, row, ctx, token, data, report)
                row.status = "paused" if report.status == "quota" else "active"
                row.last_error = "quota_exceeded" if report.status == "quota" else None
            except ConnectorAuthError:
                row.status, row.last_error = "error", "reconnect"
                report.status = "reconnect"
            except ConnectorError:
                row.last_error = "unavailable"  # temporary: try again next time
                report.status = "unavailable"
            row.last_sync_at = utcnow()
            row.files_synced += report.imported + report.updated
            row.files_skipped += report.skipped
            await c.commit()
        return report

    async def _context(self, c: AsyncSession, row: ConnectorConnection) -> WorkspaceContext | None:
        if row.created_by_user_id is None:
            return None
        found = (
            await c.execute(
                select(WorkspaceMember, Role.key, Workspace.kind)
                .join(Role, Role.id == WorkspaceMember.role_id)
                .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
                .where(
                    WorkspaceMember.workspace_id == row.workspace_id,
                    WorkspaceMember.user_id == row.created_by_user_id,
                )
            )
        ).one_or_none()
        if found is None:
            return None
        member, role, kind = found
        return WorkspaceContext(
            workspace_id=row.workspace_id,
            workspace_kind=WorkspaceKind(kind),
            user_id=member.user_id,
            member_id=member.id,
            role=RoleKey(role),
            permissions=_SYNC_PERMISSIONS,
        )

    async def _sync(
        self,
        c: AsyncSession,
        row: ConnectorConnection,
        ctx: WorkspaceContext,
        token: str,
        data: DataAccess,
        report: SyncReport,
    ) -> None:
        budget = [self._max_files]
        if row.cursor is None:
            start = await self._drive.start_page_token(token)
            known: set[str] = set()
            complete = await self._walk(row, ctx, token, data, report, known, budget)
            row.known_folders = sorted(known)
            if complete and report.status == "ok":
                row.cursor = start  # later syncs read changes from here
            return
        known = set(row.known_folders)
        page_token: str | None = row.cursor
        while page_token:
            page = await self._drive.changes(token, page_token)
            for change in page.changes:
                if budget[0] <= 0 or report.status == "quota":
                    return  # cursor not advanced: these changes are read again
                file = change.file
                if change.removed or file is None or file.trashed:
                    if file is not None and file.is_folder:
                        known.discard(file.id)
                    await self._remove(ctx, data, row, change.file_id, report)
                    continue
                inside = any(p in known for p in file.parents) or file.id in known
                if file.is_folder:
                    if inside:
                        known.add(file.id)
                    continue
                if inside:
                    await self._upsert(ctx, data, row, file, token, report, budget)
                else:
                    await self._remove(ctx, data, row, file.id, report)
            row.known_folders = sorted(known)
            if page.new_start_page_token:
                row.cursor = page.new_start_page_token
                page_token = None
            else:
                page_token = page.next_page_token
                if page_token:
                    row.cursor = page_token
            await c.commit()

    async def _walk(
        self,
        row: ConnectorConnection,
        ctx: WorkspaceContext,
        token: str,
        data: DataAccess,
        report: SyncReport,
        known: set[str],
        budget: list[int],
    ) -> bool:
        queue = [(f["id"], 0) for f in row.folders]
        while queue:
            folder_id, depth = queue.pop(0)
            if folder_id in known:
                continue
            known.add(folder_id)
            for file in await self._drive.children(token, folder_id):
                if file.is_folder:
                    if depth < _MAX_DEPTH:
                        queue.append((file.id, depth + 1))
                    continue
                if budget[0] <= 0 or report.status == "quota":
                    return False
                await self._upsert(ctx, data, row, file, token, report, budget)
        return True

    async def _upsert(
        self,
        ctx: WorkspaceContext,
        data: DataAccess,
        row: ConnectorConnection,
        file: DriveFile,
        token: str,
        report: SyncReport,
        budget: list[int],
    ) -> None:
        if file.mime_type not in SUPPORTED or (file.size or 0) > self._max_bytes:
            report.skipped += 1
            return
        async with data.sessions() as s:
            item = (
                await s.execute(
                    select(ConnectorItem).where(
                        ConnectorItem.connection_id == row.id,
                        ConnectorItem.external_id == file.id,
                    )
                )
            ).scalar_one_or_none()
            documents = data.documents(s)
            if item is not None and item.version == file.version:
                doc = await s.get(Document, item.document_id)
                if doc is not None and doc.archived_at is not None:
                    await documents.restore(ctx, doc.id)  # moved back in
                return
            budget[0] -= 1
            content = await self._drive.download(token, file)
            if len(content) > self._max_bytes:
                report.skipped += 1
                return
            try:
                if item is not None and await s.get(Document, item.document_id) is not None:
                    await documents.upload_version(
                        ctx, item.document_id, io.BytesIO(content), _filename(file)
                    )
                    report.updated += 1
                    document_id = item.document_id
                else:
                    await BillingService(s).check_upload(
                        ctx.workspace_id,
                        await BillingService(s).entitlements(ctx.workspace_id),
                        size=len(content),
                    )
                    info = await documents.upload(
                        ctx,
                        io.BytesIO(content),
                        _filename(file),
                        title=_title(file),
                        collection_id=row.collection_id,
                    )
                    report.imported += 1
                    document_id = info.id
            except QuotaExceededError:
                report.status = "quota"
                return
            except ConflictError as exc:
                if exc.code != "duplicate_document":
                    report.skipped += 1
                    report.errors.append(exc.code or "conflict")
                    return
                # Same content already in the workspace: link to it.
                checksum = hashlib.sha256(content).hexdigest()
                existing = (
                    await s.execute(
                        select(Document).where(
                            Document.workspace_id == ctx.workspace_id,
                            Document.checksum_sha256 == checksum,
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    report.skipped += 1
                    return
                if existing.archived_at is not None:
                    await documents.restore(ctx, existing.id)
                document_id = existing.id
            except DomainError as exc:
                # One bad file (wrong content, too large, encrypted…) never stops the sync.
                await s.rollback()
                report.skipped += 1
                report.errors.append(exc.code or type(exc).__name__)
                return
            if item is None:
                s.add(
                    ConnectorItem(
                        workspace_id=ctx.workspace_id,
                        connection_id=row.id,
                        external_id=file.id[:200],
                        name=file.name[:500],
                        mime_type=file.mime_type[:200],
                        version=file.version[:100],
                        document_id=document_id,
                    )
                )
            else:
                item = await s.merge(item)
                item.version, item.name, item.document_id = (
                    file.version[:100],
                    file.name[:500],
                    document_id,
                )
                item.synced_at = utcnow()
            await s.commit()

    async def _remove(
        self,
        ctx: WorkspaceContext,
        data: DataAccess,
        row: ConnectorConnection,
        external_id: str,
        report: SyncReport,
    ) -> None:
        async with data.sessions() as s:
            item = (
                await s.execute(
                    select(ConnectorItem).where(
                        ConnectorItem.connection_id == row.id,
                        ConnectorItem.external_id == external_id,
                    )
                )
            ).scalar_one_or_none()
            if item is None:
                return
            doc = await s.get(Document, item.document_id)
            if doc is not None and doc.archived_at is None:
                await data.documents(s).archive(ctx, doc.id)
                report.archived += 1
