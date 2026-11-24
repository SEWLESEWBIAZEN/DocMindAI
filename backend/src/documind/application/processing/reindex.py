"""Re-indexing when a workspace's embedding model changes (ADR 0019).

Retrieval only uses chunks embedded with the workspace's current model, so a
change of provider or embedding model needs every ready document re-embedded.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity.common import utcnow
from documind.application.ports import JobDispatcher
from documind.application.processing.jobs import new_job
from documind.core.config import Settings
from documind.infrastructure.database.models import Document, DocumentChunk
from documind.infrastructure.database.repositories.processing import (
    IndexRepository,
    ProcessingJobRepository,
)


@dataclass(frozen=True, slots=True)
class ReindexEstimate:
    target_model: str
    current_models: tuple[str, ...]
    documents: int
    chunks: int
    tokens: int

    @property
    def required(self) -> bool:
        return self.documents > 0


class ReindexService:
    """Operates on a data-plane session (the workspace's documents and chunks)."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._s = session
        self._settings = settings

    async def estimate(self, workspace_id: uuid.UUID, target_model: str) -> ReindexEstimate:
        models = (
            await self._s.execute(
                select(DocumentChunk.embedding_model)
                .where(DocumentChunk.workspace_id == workspace_id)
                .distinct()
            )
        ).scalars()
        stale = (
            await self._s.execute(
                select(
                    func.count(func.distinct(DocumentChunk.document_id)),
                    func.count(),
                    func.coalesce(func.sum(DocumentChunk.token_count), 0),
                )
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(
                    DocumentChunk.workspace_id == workspace_id,
                    DocumentChunk.embedding_model != target_model,
                    Document.status == "ready",
                )
            )
        ).one()
        return ReindexEstimate(
            target_model=target_model,
            current_models=tuple(sorted(m for m in models if m)),
            documents=int(stale[0]),
            chunks=int(stale[1]),
            tokens=int(stale[2]),
        )

    async def queue(
        self, workspace_id: uuid.UUID, target_model: str, *, requested_by: uuid.UUID | None
    ) -> list[uuid.UUID]:
        """Add processing jobs for every ready document indexed with another model.

        The caller commits, then dispatches the returned job ids.
        """
        jobs = ProcessingJobRepository(self._s)
        queued: list[uuid.UUID] = []
        documents = await IndexRepository(self._s).documents_needing_reindex(
            target_model, workspace_id=workspace_id, include_all=False
        )
        for doc in documents:
            if await jobs.active_for_document(doc.id) is not None:
                continue
            job = new_job(
                doc, self._settings, now=utcnow(), requested_by=requested_by, dispatched=True
            )
            jobs.add(job)
            doc.status = "pending"
            queued.append(job.id)
        return queued


async def dispatch_all(dispatcher: JobDispatcher, job_ids: list[uuid.UUID]) -> int:
    sent = 0
    for job_id in job_ids:
        if await dispatcher.dispatch(job_id):
            sent += 1
    return sent
