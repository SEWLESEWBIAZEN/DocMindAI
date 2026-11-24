"""Processing repositories: job state machine, extracted pages, chunks/embeddings."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.infrastructure.database.models import (
    Document,
    DocumentChunk,
    DocumentPage,
    ProcessingJob,
)
from documind.infrastructure.database.models.processing import ACTIVE_JOB_STATUSES


class ProcessingJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    def add(self, job: ProcessingJob) -> None:
        self._s.add(job)

    async def get(self, job_id: uuid.UUID, *, for_update: bool = False) -> ProcessingJob | None:
        stmt = select(ProcessingJob).where(ProcessingJob.id == job_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def claim(
        self, job_id: uuid.UUID, now: datetime, lease_seconds: int, worker: str
    ) -> ProcessingJob | None:
        """Atomically move a due, queued job to running. Only one caller can win."""
        stmt = (
            update(ProcessingJob)
            .where(
                ProcessingJob.id == job_id,
                ProcessingJob.status == "queued",
                ProcessingJob.next_attempt_at <= now,
            )
            .values(
                status="running",
                stage=None,
                attempts=ProcessingJob.attempts + 1,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                started_at=now,
                worker=worker[:128],
            )
            .returning(ProcessingJob)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def active_for_document(self, document_id: uuid.UUID) -> ProcessingJob | None:
        stmt = select(ProcessingJob).where(
            ProcessingJob.document_id == document_id,
            ProcessingJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def history(
        self, workspace_id: uuid.UUID, document_id: uuid.UUID, limit: int = 20
    ) -> list[ProcessingJob]:
        stmt = (
            select(ProcessingJob)
            .where(
                ProcessingJob.workspace_id == workspace_id,
                ProcessingJob.document_id == document_id,
            )
            .order_by(ProcessingJob.created_at.desc())
            .limit(limit)
        )
        return list((await self._s.execute(stmt)).scalars())

    async def due_for_dispatch(
        self, now: datetime, grace_seconds: int, limit: int = 200
    ) -> list[ProcessingJob]:
        """Queued jobs that are due and weren't (recently) handed to the queue."""
        stale = now - timedelta(seconds=grace_seconds)
        stmt = (
            select(ProcessingJob)
            .where(
                ProcessingJob.status == "queued",
                ProcessingJob.next_attempt_at <= now,
                (ProcessingJob.dispatched_at.is_(None)) | (ProcessingJob.dispatched_at < stale),
            )
            .order_by(ProcessingJob.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._s.execute(stmt)).scalars())

    async def claimable(
        self, workspace_id: uuid.UUID, now: datetime, limit: int = 10
    ) -> list[uuid.UUID]:
        """Due, queued job ids of a workspace, oldest first (customer workers poll this)."""
        stmt = (
            select(ProcessingJob.id)
            .where(
                ProcessingJob.workspace_id == workspace_id,
                ProcessingJob.status == "queued",
                ProcessingJob.next_attempt_at <= now,
            )
            .order_by(ProcessingJob.next_attempt_at)
            .limit(limit)
        )
        return list((await self._s.execute(stmt)).scalars())

    async def expired_leases(self, now: datetime, limit: int = 200) -> list[ProcessingJob]:
        """Running jobs whose worker stopped renewing (crashed or was killed)."""
        stmt = (
            select(ProcessingJob)
            .where(ProcessingJob.status == "running", ProcessingJob.lease_expires_at < now)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._s.execute(stmt)).scalars())

    async def documents_missing_jobs(self, limit: int = 200) -> list[Document]:
        """Pending/processing documents with no active job (e.g. uploaded before Phase 4)."""
        active = select(ProcessingJob.id).where(
            ProcessingJob.document_id == Document.id,
            ProcessingJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        stmt = (
            select(Document)
            .where(
                Document.status.in_(("pending", "processing")),
                Document.current_version_id.is_not(None),
                ~active.exists(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._s.execute(stmt)).scalars())

    async def list(self, *, status: str | None, limit: int) -> list[ProcessingJob]:
        stmt = select(ProcessingJob).order_by(ProcessingJob.updated_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(ProcessingJob.status == status)
        return list((await self._s.execute(stmt)).scalars())


class IndexRepository:
    """Extracted pages and embedded chunks, always scoped by workspace."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def replace(
        self,
        document_id: uuid.UUID,
        pages: list[DocumentPage],
        chunks: list[DocumentChunk],
    ) -> None:
        """Swap in a document's index. Older versions' rows go too: only the
        current version is searchable. Runs inside the caller's transaction."""
        await self._s.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
        await self._s.execute(delete(DocumentPage).where(DocumentPage.document_id == document_id))
        self._s.add_all(pages)
        self._s.add_all(chunks)

    async def find_embeddings(
        self, workspace_id: uuid.UUID, content_hashes: set[str], model: str, dimensions: int
    ) -> dict[str, list[float]]:
        if not content_hashes:
            return {}
        stmt = select(DocumentChunk.content_sha256, DocumentChunk.embedding).where(
            DocumentChunk.workspace_id == workspace_id,
            DocumentChunk.content_sha256.in_(content_hashes),
            DocumentChunk.embedding_model == model,
            DocumentChunk.embedding_dimensions == dimensions,
        )
        # Rows sharing a hash share the vector; the dict keeps one per hash.
        return {h: [float(x) for x in v] for h, v in await self._s.execute(stmt)}

    async def pages(
        self, workspace_id: uuid.UUID, document_id: uuid.UUID, *, offset: int, limit: int
    ) -> tuple[list[DocumentPage], int]:
        where = (DocumentPage.workspace_id == workspace_id, DocumentPage.document_id == document_id)
        total = (await self._s.execute(select(func.count()).where(*where))).scalar_one()
        rows = await self._s.execute(
            select(DocumentPage)
            .where(*where)
            .order_by(DocumentPage.position)
            .offset(offset)
            .limit(limit)
        )
        return list(rows.scalars()), int(total)

    async def chunks(
        self, workspace_id: uuid.UUID, document_id: uuid.UUID, *, offset: int, limit: int
    ) -> tuple[list[DocumentChunk], int]:
        where = (
            DocumentChunk.workspace_id == workspace_id,
            DocumentChunk.document_id == document_id,
        )
        total = (await self._s.execute(select(func.count()).where(*where))).scalar_one()
        rows = await self._s.execute(
            select(DocumentChunk)
            .where(*where)
            .order_by(DocumentChunk.chunk_index)
            .offset(offset)
            .limit(limit)
        )
        return list(rows.scalars()), int(total)

    async def documents_needing_reindex(
        self, model: str, *, workspace_id: uuid.UUID | None, include_all: bool
    ) -> list[Document]:
        stale = select(DocumentChunk.id).where(
            DocumentChunk.document_id == Document.id, DocumentChunk.embedding_model != model
        )
        stmt = select(Document).where(Document.status == "ready")
        if not include_all:
            stmt = stmt.where(stale.exists())
        if workspace_id is not None:
            stmt = stmt.where(Document.workspace_id == workspace_id)
        return list((await self._s.execute(stmt)).scalars())


class SessionEmbeddingCache:
    """EmbeddingCache backed by existing chunks (short-lived sessions)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def find_embeddings(
        self, workspace_id: uuid.UUID, content_hashes: set[str], model: str, dimensions: int
    ) -> dict[str, list[float]]:
        async with self._factory() as session:
            return await IndexRepository(session).find_embeddings(
                workspace_id, content_hashes, model, dimensions
            )
