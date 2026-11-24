"""Document collections."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity.common import AuditRecorder, RequestMeta, audit_repository
from documind.domain.errors import ConflictError, NotFoundError, ValidationFailedError
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext
from documind.infrastructure.database.models import DocumentCollection
from documind.infrastructure.database.repositories.documents import (
    CollectionRepository,
    DocumentRepository,
)

NAME_MAX = 100
DESCRIPTION_MAX = 500


@dataclass(frozen=True, slots=True)
class CollectionInfo:
    id: uuid.UUID
    name: str
    description: str | None
    document_count: int
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ValidationFailedError("Collection name can't be empty.")
    if len(cleaned) > NAME_MAX:
        raise ValidationFailedError(f"Collection name must be at most {NAME_MAX} characters.")
    return cleaned


def _clean_description(description: str | None) -> str | None:
    if description is None:
        return None
    cleaned = description.strip()
    if len(cleaned) > DESCRIPTION_MAX:
        raise ValidationFailedError(f"Description must be at most {DESCRIPTION_MAX} characters.")
    return cleaned or None


class CollectionService:
    def __init__(self, session: AsyncSession, meta: RequestMeta) -> None:
        self._session = session
        self._collections = CollectionRepository(session)
        self._documents = DocumentRepository(session)
        self._audit = AuditRecorder(audit_repository(session), meta)

    async def _require(self, ctx: WorkspaceContext, collection_id: uuid.UUID) -> DocumentCollection:
        collection = await self._collections.get(ctx.workspace_id, collection_id)
        if collection is None:
            raise NotFoundError("Collection not found.")
        return collection

    async def _info(
        self, ctx: WorkspaceContext, c: DocumentCollection, count: int | None = None
    ) -> CollectionInfo:
        if count is None:
            count = await self._collections.count_documents(ctx.workspace_id, c.id)
        return CollectionInfo(
            id=c.id,
            name=c.name,
            description=c.description,
            document_count=count,
            created_by_user_id=c.created_by_user_id,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )

    async def _commit_unique(self) -> None:
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ConflictError(
                "A collection with that name already exists.", code="duplicate_collection"
            ) from exc

    async def create(
        self, ctx: WorkspaceContext, name: str, description: str | None
    ) -> CollectionInfo:
        ctx.require(Permission.DOCUMENTS_WRITE)
        name = _clean_name(name)
        if await self._collections.name_taken(ctx.workspace_id, name):
            raise ConflictError(
                "A collection with that name already exists.", code="duplicate_collection"
            )
        collection = DocumentCollection(
            id=uuid.uuid4(),
            workspace_id=ctx.workspace_id,
            name=name,
            description=_clean_description(description),
            created_by_user_id=ctx.user_id,
        )
        self._collections.add(collection)
        self._audit.record(
            "collection.created",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="collection",
            target_id=collection.id,
            details={"name": name},
        )
        await self._commit_unique()
        await self._session.refresh(collection)
        return await self._info(ctx, collection, 0)

    async def list(self, ctx: WorkspaceContext) -> list[CollectionInfo]:
        ctx.require(Permission.DOCUMENTS_READ)
        return [
            await self._info(ctx, c, n)
            for c, n in await self._collections.list_with_counts(ctx.workspace_id)
        ]

    async def get(self, ctx: WorkspaceContext, collection_id: uuid.UUID) -> CollectionInfo:
        ctx.require(Permission.DOCUMENTS_READ)
        return await self._info(ctx, await self._require(ctx, collection_id))

    async def update(
        self,
        ctx: WorkspaceContext,
        collection_id: uuid.UUID,
        *,
        name: str | None,
        description: str | None,
        description_set: bool,
    ) -> CollectionInfo:
        ctx.require(Permission.DOCUMENTS_WRITE)
        collection = await self._require(ctx, collection_id)
        if name is not None:
            name = _clean_name(name)
            if await self._collections.name_taken(ctx.workspace_id, name, exclude=collection.id):
                raise ConflictError(
                    "A collection with that name already exists.", code="duplicate_collection"
                )
            collection.name = name
        if description_set:
            collection.description = _clean_description(description)
        self._audit.record(
            "collection.updated",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="collection",
            target_id=collection.id,
        )
        await self._commit_unique()
        await self._session.refresh(collection)
        return await self._info(ctx, collection)

    async def delete(self, ctx: WorkspaceContext, collection_id: uuid.UUID) -> None:
        """Delete the collection only; its documents stay in the workspace."""
        ctx.require(Permission.DOCUMENTS_WRITE)
        collection = await self._require(ctx, collection_id)
        name = collection.name
        await self._collections.delete(collection)
        self._audit.record(
            "collection.deleted",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="collection",
            target_id=collection_id,
            details={"name": name},
        )
        await self._session.commit()

    async def add_document(
        self, ctx: WorkspaceContext, collection_id: uuid.UUID, document_id: uuid.UUID
    ) -> None:
        ctx.require(Permission.DOCUMENTS_WRITE)
        await self._require(ctx, collection_id)
        doc = await self._documents.get(ctx.workspace_id, document_id)
        if doc is None:
            raise NotFoundError("Document not found.")
        if doc.archived_at is not None:
            raise ConflictError(
                "This document is archived. Restore it to make changes.", code="document_archived"
            )
        if await self._collections.add_document(
            ctx.workspace_id, collection_id, document_id, ctx.user_id
        ):
            self._audit.record(
                "collection.document_added",
                actor_user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                target_type="collection",
                target_id=collection_id,
                details={"document_id": str(document_id)},
            )
        await self._session.commit()

    async def remove_document(
        self, ctx: WorkspaceContext, collection_id: uuid.UUID, document_id: uuid.UUID
    ) -> None:
        ctx.require(Permission.DOCUMENTS_WRITE)
        await self._require(ctx, collection_id)
        if not await self._collections.remove_document(
            ctx.workspace_id, collection_id, document_id
        ):
            raise NotFoundError("That document isn't in this collection.")
        self._audit.record(
            "collection.document_removed",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="collection",
            target_id=collection_id,
            details={"document_id": str(document_id)},
        )
        await self._session.commit()
