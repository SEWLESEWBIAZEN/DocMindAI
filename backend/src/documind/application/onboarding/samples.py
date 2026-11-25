"""Sample documents for trying DocuMind without uploading anything (ADR 0033).

The samples are DocuMind's own fictional documents, bundled with the backend.
They go through the normal upload pipeline (storage, scanning, processing,
quotas, audit), so the first answer a new user sees is a real one. A sample is
recognised by its content hash; one that was never changed can be removed by
anyone who can upload, without the 2FA step needed to delete real documents.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import cache
from importlib import resources

from documind.application.documents.collection_service import CollectionService
from documind.application.documents.document_service import DocumentService
from documind.domain.errors import ConflictError
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext

COLLECTION_NAME = "Sample documents"
COLLECTION_DESCRIPTION = "Fictional documents from DocuMind for trying things out. Safe to remove."

#: Questions the samples answer well, shown as suggestions in the chat.
QUESTIONS = (
    "How much notice is needed for non-renewal of the services agreement?",
    "How quickly must a personal-data breach be notified, and to whom?",
    "How many days of annual leave do employees get, and how many unused days carry over?",
    "How do I do a factory reset of the Aurora thermostat?",
    "Within how many days are invoices payable under the services agreement?",
)


@dataclass(frozen=True, slots=True)
class SampleFile:
    filename: str
    title: str
    content: bytes

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


_FILES = (
    ("services-agreement.md", "Sample: Master Services Agreement"),
    ("employee-handbook.md", "Sample: Employee Handbook"),
    ("product-faq.md", "Sample: Aurora Thermostat FAQ"),
)


@cache
def sample_files() -> tuple[SampleFile, ...]:
    package = resources.files("documind.resources.samples")
    return tuple(
        SampleFile(name, title, package.joinpath(name).read_bytes()) for name, title in _FILES
    )


@cache
def sample_checksums() -> frozenset[str]:
    return frozenset(f.checksum for f in sample_files())


def is_sample(checksum: str, version_count: int) -> bool:
    """Unchanged sample: same content and no versions uploaded by a user."""
    return version_count == 1 and checksum in sample_checksums()


@dataclass(frozen=True, slots=True)
class SampleStatus:
    document_ids: list[uuid.UUID]
    collection_id: uuid.UUID | None
    questions: tuple[str, ...] = QUESTIONS

    @property
    def loaded(self) -> bool:
        return bool(self.document_ids)


class SampleService:
    def __init__(self, documents: DocumentService, collections: CollectionService) -> None:
        self._documents = documents
        self._collections = collections

    async def status(self, ctx: WorkspaceContext) -> SampleStatus:
        ctx.require(Permission.DOCUMENTS_READ)
        ids = await self._documents.ids_by_checksum(ctx, sample_checksums())
        collection = next(
            (c for c in await self._collections.list(ctx) if c.name == COLLECTION_NAME), None
        )
        return SampleStatus(ids, collection.id if collection else None)

    async def load(
        self,
        ctx: WorkspaceContext,
        admit: Callable[[int], Awaitable[None]] | None = None,
    ) -> SampleStatus:
        """Add any samples not in the workspace yet. Idempotent.

        ``admit(size)`` runs before each upload (plan quotas), like a normal upload.
        """
        ctx.require(Permission.DOCUMENTS_WRITE)
        present = set(await self._documents.checksums_present(ctx, sample_checksums()))
        missing = [f for f in sample_files() if f.checksum not in present]
        if missing:
            collection_id = await self._collection(ctx)
            for sample in missing:
                if admit is not None:
                    await admit(len(sample.content))
                try:
                    await self._documents.upload(
                        ctx,
                        io.BytesIO(sample.content),
                        sample.filename,
                        title=sample.title,
                        collection_id=collection_id,
                    )
                except ConflictError as exc:
                    if exc.code != "duplicate_document":  # added concurrently: fine
                        raise
        return await self.status(ctx)

    async def remove(self, ctx: WorkspaceContext) -> int:
        """Delete unchanged samples and the sample collection if it ends up empty."""
        ctx.require(Permission.DOCUMENTS_WRITE)
        removed = 0
        for document_id in await self._documents.ids_by_checksum(ctx, sample_checksums()):
            if await self._documents.delete_sample(ctx, document_id):
                removed += 1
        for collection in await self._collections.list(ctx):
            if collection.name == COLLECTION_NAME and collection.document_count == 0:
                await self._collections.delete(ctx, collection.id)
        return removed

    async def _collection(self, ctx: WorkspaceContext) -> uuid.UUID:
        for collection in await self._collections.list(ctx):
            if collection.name == COLLECTION_NAME:
                return collection.id
        try:
            return (await self._collections.create(ctx, COLLECTION_NAME, COLLECTION_DESCRIPTION)).id
        except ConflictError:  # created concurrently
            for collection in await self._collections.list(ctx):
                if collection.name == COLLECTION_NAME:
                    return collection.id
            raise
