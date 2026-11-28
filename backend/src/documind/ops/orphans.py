"""Find (and optionally delete) objects in the platform bucket that no row refers to.

Production readiness F6/F7. Objects can be orphaned when:

* a document is deleted, the database commits, and the object delete then fails;
* an upload writes the object, the database transaction then fails, and so does
  the clean-up delete;
* a data export's row is removed or expired while its file stays behind.

Rules (the platform bucket only; a customer's own bucket is never touched):

* ``workspaces/<workspace id>/documents/...`` is referenced if a ``document_versions``
  row in that workspace's **current** data plane has it as ``storage_key``. Skipped:
  workspaces being migrated, and workspaces whose storage moved to their own bucket
  (the platform copy is a retained migration source with its own clean-up).
* ``exports/...`` is referenced if a ``data_exports`` row that isn't ``expired`` has
  it as ``object_key``.
* Objects newer than ``min_age`` are never orphans (an upload may still be committing).
* Any other prefix is reported as unknown and never deleted.

Without ``delete`` nothing is changed. Keys are reported; file contents never are.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from documind.application.infrastructure.resolver import (
    InfrastructureResolver,
    WorkspaceMigratingError,
)
from documind.infrastructure.database.models import AgreementFile, DataExport, DocumentVersion

logger = logging.getLogger(__name__)

DOCUMENTS, EXPORTS = "workspaces/", "exports/"


@dataclass
class OrphanReport:
    scanned: int = 0
    referenced: int = 0
    too_new: int = 0
    orphans: list[tuple[str, int]] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    unknown: list[str] = field(default_factory=list)
    deleted: int = 0
    delete_failed: int = 0

    def as_dict(self, *, list_limit: int = 200) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "referenced": self.referenced,
            "too_new": self.too_new,
            "orphans": len(self.orphans),
            "orphan_bytes": sum(size for _, size in self.orphans),
            "orphan_keys": [k for k, _ in self.orphans[:list_limit]],
            "skipped": dict(self.skipped),
            "unknown_prefix": self.unknown[:list_limit],
            "deleted": self.deleted,
            "delete_failed": self.delete_failed,
        }


def _pages(client: Any, bucket: str) -> Iterator[list[dict[str, Any]]]:
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        yield page.get("Contents", [])


class OrphanReconciler:
    def __init__(
        self,
        *,
        s3_client: Any,
        bucket: str,
        control: Any,
        resolver: InfrastructureResolver,
    ) -> None:
        self._s3 = s3_client
        self._bucket = bucket
        self._control = control
        self._resolver = resolver
        # workspace id -> referenced keys, or a reason the workspace is skipped
        self._workspaces: dict[uuid.UUID, set[str] | str] = {}
        self._exports: set[str] | None = None

    async def run(
        self, *, min_age: timedelta = timedelta(hours=24), delete: bool = False
    ) -> OrphanReport:
        report = OrphanReport()
        cutoff = datetime.now(UTC) - min_age
        pages = await asyncio.to_thread(lambda: iter(_pages(self._s3, self._bucket)))
        while True:
            page = await asyncio.to_thread(next, pages, None)
            if page is None:
                break
            for obj in page:
                await self._classify(obj, cutoff, report)
        if delete:
            for key, _ in report.orphans:
                try:
                    await asyncio.to_thread(self._s3.delete_object, Bucket=self._bucket, Key=key)
                    report.deleted += 1
                except Exception:
                    report.delete_failed += 1
                    logger.warning("orphan delete failed", extra={"key": key})
        logger.info(
            "orphaned-object reconciliation finished",
            extra={k: v for k, v in report.as_dict().items() if not isinstance(v, list)},
        )
        return report

    async def _classify(self, obj: dict[str, Any], cutoff: datetime, report: OrphanReport) -> None:
        key, size = str(obj["Key"]), int(obj.get("Size", 0))
        report.scanned += 1
        if key.startswith(DOCUMENTS):
            referenced = await self._document_referenced(key)
        elif key.startswith(EXPORTS):
            referenced = key in await self._export_keys()
        else:
            report.unknown.append(key)
            return
        if isinstance(referenced, str):  # the workspace is skipped, with this reason
            report.skipped[referenced] += 1
        elif referenced:
            report.referenced += 1
        elif obj["LastModified"] > cutoff:
            report.too_new += 1
        else:
            report.orphans.append((key, size))

    async def _document_referenced(self, key: str) -> bool | str:
        parts = key.split("/")
        try:
            ws = uuid.UUID(parts[1])
        except (IndexError, ValueError):
            return "unparseable_key"
        known = self._workspaces.get(ws)
        if known is None:
            known = await self._load_workspace(ws)
            self._workspaces[ws] = known
        return known if isinstance(known, str) else key in known

    async def _load_workspace(self, ws: uuid.UUID) -> set[str] | str:
        try:
            infra = await self._resolver.resolve(ws)
        except WorkspaceMigratingError:
            return "workspace_migrating"
        except Exception:
            logger.warning("orphan check: workspace unreachable", extra={"workspace_id": str(ws)})
            return "workspace_unreachable"
        if infra.config.storage.provider != "documind":
            return "retained_migration_source"
        async with infra.data_plane.sessions() as session:
            documents = await session.execute(
                select(DocumentVersion.storage_key).where(DocumentVersion.workspace_id == ws)
            )
            # Signing evidence (snapshots, signed PDFs, certificates, signature images) lives
            # under workspaces/{ws}/agreements/ and is referenced by agreement_files.
            agreements = await session.execute(
                select(AgreementFile.storage_key).where(AgreementFile.workspace_id == ws)
            )
            return {str(k) for (k,) in documents} | {str(k) for (k,) in agreements}

    async def _export_keys(self) -> set[str]:
        if self._exports is None:
            async with self._control() as session:
                rows = await session.execute(
                    select(DataExport.object_key).where(
                        DataExport.object_key.is_not(None), DataExport.status != "expired"
                    )
                )
                self._exports = {str(k) for (k,) in rows}
        return self._exports
