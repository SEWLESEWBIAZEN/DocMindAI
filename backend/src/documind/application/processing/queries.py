"""Read access to processing state and results (workspace-scoped)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from documind.domain.errors import NotFoundError
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext
from documind.infrastructure.database.models import (
    Document,
    DocumentChunk,
    DocumentPage,
    ProcessingJob,
)
from documind.infrastructure.database.repositories.documents import DocumentRepository
from documind.infrastructure.database.repositories.processing import (
    IndexRepository,
    ProcessingJobRepository,
)


@dataclass(frozen=True, slots=True)
class ProcessingStatus:
    document: Document
    jobs: list[ProcessingJob]


class ProcessingQueries:
    def __init__(self, session: AsyncSession) -> None:
        self._documents = DocumentRepository(session)
        self._jobs = ProcessingJobRepository(session)
        self._index = IndexRepository(session)

    async def _document(self, ctx: WorkspaceContext, document_id: uuid.UUID) -> Document:
        ctx.require(Permission.DOCUMENTS_READ)
        doc = await self._documents.get(ctx.workspace_id, document_id)
        if doc is None:
            raise NotFoundError("Document not found.")
        return doc

    async def status(self, ctx: WorkspaceContext, document_id: uuid.UUID) -> ProcessingStatus:
        doc = await self._document(ctx, document_id)
        return ProcessingStatus(doc, await self._jobs.history(ctx.workspace_id, doc.id))

    async def pages(
        self, ctx: WorkspaceContext, document_id: uuid.UUID, *, offset: int, limit: int
    ) -> tuple[list[DocumentPage], int]:
        doc = await self._document(ctx, document_id)
        return await self._index.pages(ctx.workspace_id, doc.id, offset=offset, limit=limit)

    async def chunks(
        self, ctx: WorkspaceContext, document_id: uuid.UUID, *, offset: int, limit: int
    ) -> tuple[list[DocumentChunk], int]:
        doc = await self._document(ctx, document_id)
        return await self._index.chunks(ctx.workspace_id, doc.id, offset=offset, limit=limit)
