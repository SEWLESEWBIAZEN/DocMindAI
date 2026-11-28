"""Approved infrastructure migrations (MI-8, ADR 0025).

Moves one workspace's data between data planes and/or object stores:

1. **Lock.** The migration is marked ``running``. The resolver then reports the
   workspace as migrating: requests get 503 ``migration_in_progress`` and
   workers leave its jobs queued. The executor waits for every process's
   routing cache to expire and for running jobs to finish.
2. **Preflight.** The destination must hold nothing for this workspace, so a
   rollback can only ever delete what this run copied.
3. **Copy.** Rows are copied table by table in dependency order, in batches;
   objects key by key, unchanged.
4. **Verify.** Row counts per table, and the size of every object, must match.
5. **Switch.** The workspace is pinned to the target version; the old one is retired.
6. **Retain.** The source copy stays until an owner runs the separate cleanup.

On any failure the partial copy at the destination is removed, the source is
untouched and the workspace is unlocked; the migration is ``failed``.

**Crash recovery (production readiness F3).** A run holds a lease: ``plan.runner``
plus ``plan.heartbeat_at``, refreshed between tables and every few files. Each phase
is recorded when it finishes (``rows_copied``, ``objects_copied``, ``verified``). If
the process dies, ``resume`` takes over once the heartbeat is older than the lease:

* rows are copied in one destination transaction, so an unfinished row phase left
  nothing committed; resume clears this workspace's rows at the destination (only
  this run's copy, since preflight proved it held none) and copies again;
* files are re-copied, skipping those already there with the right size;
* the copy is verified again before the switch; a crash after the switch only
  marks the migration completed.

A runner that loses its lease (another process resumed it) stops without cleaning
up, so it never deletes the copy the new runner is using.
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import Table, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.credentials.vault import CredentialVault
from documind.application.identity.common import AuditRecorder, RequestMeta, utcnow
from documind.application.infrastructure.resolver import (
    InfrastructureResolver,
    ResolvedInfrastructure,
    verify,
)
from documind.application.ports import ObjectNotFoundError
from documind.domain.errors import ConflictError, NotFoundError
from documind.infrastructure.database.base import Base
from documind.infrastructure.database.dataplane import DATA_PLANE_TABLES
from documind.infrastructure.database.dataplane_runtime import register_tenant
from documind.infrastructure.database.models import (
    AgreementFile,
    Document,
    DocumentVersion,
    InfrastructureEvent,
    InfrastructureMigration,
    InfrastructureProfileVersion,
    ProcessingJob,
    WorkspaceInfrastructure,
)
from documind.infrastructure.database.repositories.identity import AuditLogRepository

logger = logging.getLogger(__name__)

BATCH = 500
#: A run whose heartbeat is older than this can be resumed by another process.
LEASE_SECONDS = 300
_OBJECT_BEAT_EVERY = 25
# Messages reference earlier messages (reply_to_id): copy them in conversation order.
_ORDER = {"messages": ("conversation_id", "sequence")}


class MigrationFailedError(Exception):
    pass


class DestinationNotEmptyError(MigrationFailedError):
    pass


class LeaseLostError(Exception):
    """Another process resumed this migration: stop, and leave its copy alone."""


@dataclass(slots=True)
class MigrationReport:
    rows: dict[str, int] = field(default_factory=dict)
    objects: int = 0
    object_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"rows": self.rows, "objects": self.objects, "object_bytes": self.object_bytes}


def _tables() -> list[Table]:
    return [Base.metadata.tables[name] for name in DATA_PLANE_TABLES]


def _insertable(table: Table) -> list[str]:
    return [c.name for c in table.columns if c.computed is None]  # skips search_tsv


async def _object_keys(
    sessions: async_sessionmaker[AsyncSession], ws: uuid.UUID
) -> list[tuple[str, int]]:
    async with sessions() as data:
        rows = list(
            (
                await data.execute(
                    select(DocumentVersion.storage_key, DocumentVersion.size_bytes).where(
                        DocumentVersion.workspace_id == ws
                    )
                )
            ).all()
        )
        # DocuMind Sign evidence files move with the workspace too.
        rows += (
            await data.execute(
                select(AgreementFile.storage_key, AgreementFile.size_bytes).where(
                    AgreementFile.workspace_id == ws
                )
            )
        ).all()
    return [(k, int(s)) for k, s in rows]


async def _row_count(session: AsyncSession, table: Table, ws: uuid.UUID) -> int:
    count = select(func.count()).select_from(table).where(table.c.workspace_id == ws)
    return int((await session.execute(count)).scalar_one())


class MigrationExecutor:
    def __init__(
        self,
        control: async_sessionmaker[AsyncSession],
        resolver: InfrastructureResolver,
        *,
        drain_seconds: float | None = None,
        wait_for_jobs_seconds: float = 900.0,
    ) -> None:
        self._control = control
        self._resolver = resolver
        # Other API and worker processes route from a cache: wait it out.
        self._drain = resolver.cache_seconds + 1 if drain_seconds is None else drain_seconds
        self._wait_jobs = wait_for_jobs_seconds

    # ----------------------------------------------------------------- run

    async def run(self, migration_id: uuid.UUID) -> MigrationReport:
        async with self._control() as session:
            migration = await session.get(
                InfrastructureMigration, migration_id, with_for_update=True
            )
            if migration is None:
                raise NotFoundError("Migration not found.")
            if migration.status != "approved":
                raise ConflictError(
                    "Only approved migrations can run.", code="migration_not_approved"
                )
            runner = uuid.uuid4().hex
            now = utcnow().isoformat()
            migration.status = "running"
            migration.plan = {
                **migration.plan,
                "started_at": now,
                "runner": runner,
                "heartbeat_at": now,
            }
            ws, target_id = migration.workspace_id, migration.to_version_id
            moves = list(migration.plan.get("moves", []))
            await session.commit()
        self._resolver.invalidate(ws)
        await self._event(ws, target_id, "migration_started", None, moves=moves)
        return await self._execute(migration_id, ws, target_id, moves, runner, {})

    async def resume(
        self, migration_id: uuid.UUID, *, lease_seconds: float = LEASE_SECONDS
    ) -> MigrationReport:
        """Continue a run whose process died (status ``running``, heartbeat stale)."""
        async with self._control() as session:
            migration = await session.get(
                InfrastructureMigration, migration_id, with_for_update=True
            )
            if migration is None:
                raise NotFoundError("Migration not found.")
            if migration.status != "running":
                raise ConflictError("Only a running migration can be resumed.", code="not_running")
            beat = migration.plan.get("heartbeat_at")
            if beat is not None:
                from datetime import datetime

                age = (utcnow() - datetime.fromisoformat(beat)).total_seconds()
                if age < lease_seconds:
                    raise ConflictError(
                        f"This migration is still running (last heartbeat {int(age)} s ago).",
                        code="migration_active",
                    )
            runner = uuid.uuid4().hex
            now = utcnow().isoformat()
            done = dict(migration.plan)
            migration.plan = {
                **migration.plan,
                "runner": runner,
                "heartbeat_at": now,
                "resumed": int(migration.plan.get("resumed", 0)) + 1,
                "resumed_at": now,
            }
            ws, target_id = migration.workspace_id, migration.to_version_id
            moves = list(migration.plan.get("moves", []))
            await session.commit()
        await self._event(ws, target_id, "migration_resumed", None, moves=moves)
        return await self._execute(migration_id, ws, target_id, moves, runner, done)

    async def _execute(
        self,
        migration_id: uuid.UUID,
        ws: uuid.UUID,
        target_id: uuid.UUID,
        moves: Sequence[str],
        runner: str,
        done: dict[str, Any],
    ) -> MigrationReport:
        """Copy, verify and switch, skipping the phases ``done`` records as finished."""
        resuming = bool(done)
        report = MigrationReport()
        copied = False
        source: ResolvedInfrastructure | None = None
        target: ResolvedInfrastructure | None = None
        try:
            if resuming and await self._already_switched(ws, target_id):
                await self._finish(migration_id, "completed", report)
                return report
            source = await self._resolver.resolve(ws, ignore_migration=True)
            target = await self._resolver.build_version(ws, target_id)
            await asyncio.sleep(self._drain)
            await self._wait_for_running_jobs(source, ws)
            await self._beat(migration_id, runner)
            if not done.get("preflight_passed"):
                await self._preflight(source, target, ws, moves)
                await self._note(migration_id, preflight_passed=True)
            copied = True
            if "database" in moves and not done.get("rows_copied"):
                if target.isolated:
                    await register_tenant(target.data_plane.sessions, ws)
                if resuming:  # an unfinished row phase committed nothing; make sure
                    await self._delete_rows(target.data_plane.sessions, ws)
                await self._copy_rows(source, target, ws, report, migration_id, runner)
                await self._note(migration_id, rows_copied=True)
            if "storage" in moves and not done.get("objects_copied"):
                await self._copy_objects(
                    source, target, ws, report, migration_id, runner, skip_existing=resuming
                )
                await self._note(migration_id, objects_copied=True)
            await self._beat(migration_id, runner)
            await self._verify(source, target, ws, moves)
            await self._note(migration_id, verified=True)
        except LeaseLostError:
            logger.warning(
                "migration taken over by another runner; stopping without cleanup",
                extra={"migration_id": str(migration_id)},
            )
            raise
        except Exception as exc:
            logger.exception(
                "infrastructure migration failed", extra={"migration_id": str(migration_id)}
            )
            if copied and source is not None and target is not None:
                await self._remove_copy(source, target, ws, moves)
            await self._finish(migration_id, "failed", report, error=str(exc)[:500])
            raise MigrationFailedError(str(exc)) from exc
        await self._switch(migration_id, ws, target_id, report)
        return report

    async def _already_switched(self, ws: uuid.UUID, target_id: uuid.UUID) -> bool:
        async with self._control() as session:
            pin = await session.get(WorkspaceInfrastructure, ws)
            return pin is not None and pin.version_id == target_id

    async def _beat(self, migration_id: uuid.UUID, runner: str) -> None:
        """Refresh the lease; raise LeaseLostError if another runner holds it now."""
        async with self._control() as session:
            migration = await session.get(
                InfrastructureMigration, migration_id, with_for_update=True
            )
            if migration is None or migration.plan.get("runner") != runner:
                raise LeaseLostError(str(migration_id))
            migration.plan = {**migration.plan, "heartbeat_at": utcnow().isoformat()}
            await session.commit()

    async def abort(self, migration_id: uuid.UUID) -> None:
        """Operator recovery for a run whose process died (status stuck at ``running``)."""
        async with self._control() as session:
            migration = await session.get(InfrastructureMigration, migration_id)
            if migration is None:
                raise NotFoundError("Migration not found.")
            if migration.status != "running":
                raise ConflictError("Only a running migration can be aborted.", code="not_running")
            ws, target_id = migration.workspace_id, migration.to_version_id
            moves = list(migration.plan.get("moves", []))
            copied = bool(migration.plan.get("preflight_passed"))
        if copied:
            source = await self._resolver.resolve(ws, ignore_migration=True)
            target = await self._resolver.build_version(ws, target_id)
            await self._remove_copy(source, target, ws, moves)
        await self._finish(migration_id, "failed", MigrationReport(), error="aborted by operator")

    # ------------------------------------------------------------- cleanup

    async def cleanup_source(
        self, migration_id: uuid.UUID, *, actor: uuid.UUID | None, meta: RequestMeta | None = None
    ) -> MigrationReport:
        """Delete the retained source copy of a completed migration (explicit, audited)."""
        async with self._control() as session:
            migration = await session.get(InfrastructureMigration, migration_id)
            if migration is None:
                raise NotFoundError("Migration not found.")
            if migration.status != "completed" or migration.plan.get("source_cleaned"):
                raise ConflictError(
                    "Nothing to clean up for this migration.", code="nothing_to_clean"
                )
            ws, from_version = migration.workspace_id, migration.from_version_id
            moves = list(migration.plan.get("moves", []))
            pin = await session.get(WorkspaceInfrastructure, ws)
            if pin is None or pin.version_id != migration.to_version_id:
                # A later change moved the workspace again: the "source" may be live data.
                raise ConflictError(
                    "The workspace has moved since this migration; nothing was deleted.",
                    code="migration_superseded",
                )
        source = (
            await self._resolver.build_version(ws, from_version)
            if from_version
            else self._resolver.platform(ws)
        )
        active = await self._resolver.resolve(ws, ignore_migration=True)
        report = MigrationReport()
        if "storage" in moves and source.storage is not active.storage:
            for key, _ in await _object_keys(active.data_plane.sessions, ws):
                await source.storage.delete(key)
                report.objects += 1
        if "database" in moves and source.data_plane.key != active.data_plane.key:
            report.rows = await self._delete_rows(source.data_plane.sessions, ws)
        async with self._control() as session:
            migration = await session.get(
                InfrastructureMigration, migration_id, with_for_update=True
            )
            assert migration is not None  # noqa: S101 - read above
            # Secrets only the old location used are destroyed with its data.
            keep = {
                active.config.database.password_credential_id,
                active.config.storage.secret_credential_id,
            }
            for cid in (
                source.config.database.password_credential_id,
                source.config.storage.secret_credential_id,
            ):
                if cid is not None and cid not in keep:
                    cred = await CredentialVault(session, None).get(ws, cid)
                    if cred is not None and cred.status == "active":
                        CredentialVault.revoke(cred)
            migration.plan = {**migration.plan, "source_cleaned": True, "cleanup": report.as_dict()}
            session.add(
                InfrastructureEvent(
                    workspace_id=ws,
                    version_id=migration.to_version_id,
                    kind="migration_source_cleaned",
                    ok=True,
                    details=report.as_dict(),
                )
            )
            AuditRecorder(AuditLogRepository(session), meta or RequestMeta()).record(
                "infrastructure.migration_source_cleaned",
                actor_user_id=actor,
                workspace_id=ws,
                target_type="infrastructure_migration",
                target_id=migration_id,
                details=report.as_dict(),
            )
            await session.commit()
        return report

    # ------------------------------------------------------------- helpers

    async def _event(
        self,
        ws: uuid.UUID,
        version_id: uuid.UUID | None,
        kind: str,
        ok: bool | None,
        **details: Any,
    ) -> None:
        async with self._control() as session:
            session.add(
                InfrastructureEvent(
                    workspace_id=ws, version_id=version_id, kind=kind, ok=ok, details=details
                )
            )
            await session.commit()

    async def _note(self, migration_id: uuid.UUID, **values: Any) -> None:
        async with self._control() as session:
            migration = await session.get(
                InfrastructureMigration, migration_id, with_for_update=True
            )
            assert migration is not None  # noqa: S101 - exists
            migration.plan = {**migration.plan, **values}
            await session.commit()

    async def _wait_for_running_jobs(self, source: ResolvedInfrastructure, ws: uuid.UUID) -> None:
        deadline = utcnow() + timedelta(seconds=self._wait_jobs)
        while True:
            async with source.data_plane.sessions() as data:
                running = (
                    await data.execute(
                        select(func.count())
                        .select_from(ProcessingJob)
                        .where(ProcessingJob.workspace_id == ws, ProcessingJob.status == "running")
                    )
                ).scalar_one()
            if not running:
                return
            if utcnow() > deadline:
                raise MigrationFailedError("Processing jobs are still running.")
            await asyncio.sleep(2)

    async def _preflight(
        self,
        source: ResolvedInfrastructure,
        target: ResolvedInfrastructure,
        ws: uuid.UUID,
        moves: Sequence[str],
    ) -> None:
        if "database" in moves:
            if source.data_plane.key == target.data_plane.key:
                raise MigrationFailedError("Source and destination database are the same.")
            async with target.data_plane.sessions() as dst:
                for table in _tables():
                    if await _row_count(dst, table, ws):
                        raise DestinationNotEmptyError(
                            f"The destination database already has {table.name} rows "
                            "for this workspace."
                        )
        if "storage" in moves:
            if source.storage is target.storage:
                raise MigrationFailedError("Source and destination storage are the same.")
            for key, _ in await _object_keys(source.data_plane.sessions, ws):
                try:
                    await target.storage.get_metadata(key)
                except ObjectNotFoundError:
                    continue
                raise DestinationNotEmptyError("The destination storage already has these files.")

    async def _copy_rows(
        self,
        source: ResolvedInfrastructure,
        target: ResolvedInfrastructure,
        ws: uuid.UUID,
        report: MigrationReport,
        migration_id: uuid.UUID | None = None,
        runner: str | None = None,
    ) -> None:
        async with source.data_plane.sessions() as src, target.data_plane.sessions() as dst:
            for table in _tables():
                if migration_id is not None and runner is not None:
                    await self._beat(migration_id, runner)
                columns = _insertable(table)
                order = [table.c[c] for c in _ORDER.get(table.name, ())] or list(
                    table.primary_key.columns
                )
                offset = copied = 0
                while True:
                    rows = (
                        (
                            await src.execute(
                                select(*[table.c[c] for c in columns])
                                .where(table.c.workspace_id == ws)
                                .order_by(*order)
                                .offset(offset)
                                .limit(BATCH)
                            )
                        )
                        .mappings()
                        .all()
                    )
                    if not rows:
                        break
                    values = [dict(r) for r in rows]
                    if table.name == "documents":
                        # documents and versions reference each other: link after versions.
                        for v in values:
                            v["current_version_id"] = None
                    await dst.execute(table.insert(), values)
                    copied += len(values)
                    offset += BATCH
                report.rows[table.name] = copied
            links = (
                await src.execute(
                    select(Document.id, Document.current_version_id).where(
                        Document.workspace_id == ws, Document.current_version_id.is_not(None)
                    )
                )
            ).all()
            for doc_id, version_id in links:
                await dst.execute(
                    update(Document)
                    .where(Document.id == doc_id)
                    .values(current_version_id=version_id)
                )
            await dst.commit()

    async def _copy_objects(
        self,
        source: ResolvedInfrastructure,
        target: ResolvedInfrastructure,
        ws: uuid.UUID,
        report: MigrationReport,
        migration_id: uuid.UUID | None = None,
        runner: str | None = None,
        *,
        skip_existing: bool = False,
    ) -> None:
        for n, (key, size) in enumerate(await _object_keys(source.data_plane.sessions, ws)):
            if migration_id is not None and runner is not None and n % _OBJECT_BEAT_EVERY == 0:
                await self._beat(migration_id, runner)
            if skip_existing:
                try:
                    if (await target.storage.get_metadata(key)).size == size:
                        report.objects += 1
                        report.object_bytes += size
                        continue
                except ObjectNotFoundError:
                    pass
            meta = await source.storage.get_metadata(key)
            buffer = io.BytesIO()
            await source.storage.download_to(key, buffer)
            buffer.seek(0)
            await target.storage.upload(
                key,
                buffer,
                content_type=meta.content_type or "application/octet-stream",
                metadata=dict(meta.metadata),
            )
            report.objects += 1
            report.object_bytes += meta.size

    async def _verify(
        self,
        source: ResolvedInfrastructure,
        target: ResolvedInfrastructure,
        ws: uuid.UUID,
        moves: Sequence[str],
    ) -> None:
        if "database" in moves:
            async with source.data_plane.sessions() as src, target.data_plane.sessions() as dst:
                for table in _tables():
                    a, b = await _row_count(src, table, ws), await _row_count(dst, table, ws)
                    if a != b:
                        raise MigrationFailedError(
                            f"{table.name}: {a} rows at the source, {b} at the destination"
                        )
        if "storage" in moves:
            for key, size in await _object_keys(source.data_plane.sessions, ws):
                if (await target.storage.get_metadata(key)).size != size:
                    raise MigrationFailedError(f"Object size mismatch for {key}")

    @staticmethod
    async def _delete_rows(
        sessions: async_sessionmaker[AsyncSession], ws: uuid.UUID
    ) -> dict[str, int]:
        deleted: dict[str, int] = {}
        async with sessions() as data:
            await data.execute(
                update(Document).where(Document.workspace_id == ws).values(current_version_id=None)
            )
            for table in reversed(_tables()):
                result = await data.execute(delete(table).where(table.c.workspace_id == ws))
                deleted[table.name] = int(result.rowcount or 0)  # type: ignore[attr-defined]
            await data.commit()
        return deleted

    async def _remove_copy(
        self,
        source: ResolvedInfrastructure,
        target: ResolvedInfrastructure,
        ws: uuid.UUID,
        moves: Sequence[str],
    ) -> None:
        """Undo a failed run. Preflight proved the destination held nothing for ``ws``."""
        try:
            if "storage" in moves:
                for key, _ in await _object_keys(source.data_plane.sessions, ws):
                    await target.storage.delete(key)
            if "database" in moves:
                await self._delete_rows(target.data_plane.sessions, ws)
        except Exception:
            logger.exception("could not remove the partial copy; the source is untouched")

    async def _finish(
        self,
        migration_id: uuid.UUID,
        status: str,
        report: MigrationReport,
        *,
        error: str | None = None,
    ) -> None:
        async with self._control() as session:
            migration = await session.get(
                InfrastructureMigration, migration_id, with_for_update=True
            )
            assert migration is not None  # noqa: S101 - exists
            migration.status = status
            migration.plan = {
                **migration.plan,
                "finished_at": utcnow().isoformat(),
                "report": report.as_dict(),
                **({"error": error} if error else {}),
            }
            ws = migration.workspace_id
            session.add(
                InfrastructureEvent(
                    workspace_id=ws,
                    version_id=migration.to_version_id,
                    kind=f"migration_{status}",
                    ok=status == "completed",
                    details={"report": report.as_dict(), "error": error},
                )
            )
            await session.commit()
        self._resolver.invalidate(ws)

    async def _switch(
        self, migration_id: uuid.UUID, ws: uuid.UUID, target_id: uuid.UUID, report: MigrationReport
    ) -> None:
        async with self._control() as session:
            target = await session.get(
                InfrastructureProfileVersion, target_id, with_for_update=True
            )
            assert target is not None  # noqa: S101 - foreign key
            verify(target)
            pin = await session.get(WorkspaceInfrastructure, ws, with_for_update=True)
            previous = pin.version_id if pin is not None else None
            if pin is None:
                session.add(WorkspaceInfrastructure(workspace_id=ws, version_id=target_id))
            else:
                old = await session.get(InfrastructureProfileVersion, pin.version_id)
                if old is not None:
                    old.status = "retired"
                pin.version_id = target_id
            # Source credentials are kept: the retained copy is still readable for cleanup.
            target.status = "active"
            target.activated_at = utcnow()
            AuditRecorder(AuditLogRepository(session), RequestMeta()).record(
                "infrastructure.migration_completed",
                actor_user_id=None,
                workspace_id=ws,
                target_type="infrastructure_migration",
                target_id=migration_id,
                details={**report.as_dict(), "previous": str(previous) if previous else None},
            )
            await session.commit()
        await self._finish(migration_id, "completed", report)
