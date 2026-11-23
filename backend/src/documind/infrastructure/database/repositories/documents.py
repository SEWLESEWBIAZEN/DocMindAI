"""Document repositories. Every query is scoped by ``workspace_id``."""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from documind.infrastructure.database.models import (
    Document,
    DocumentCollection,
    DocumentCollectionItem,
    DocumentVersion,
)

SortField = Literal["created_at", "updated_at", "title", "size_bytes"]
ArchivedFilter = Literal["exclude", "only", "include"]


@dataclass(frozen=True, slots=True)
class DocumentFilter:
    query: str | None = None
    status: str | None = None
    format: str | None = None
    collection_id: uuid.UUID | None = None
    uploaded_by: uuid.UUID | None = None
    archived: ArchivedFilter = "exclude"
    sort: SortField = "created_at"
    descending: bool = True
    limit: int = 25
    offset: int = 0


def _like_pattern(text: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    def add(self, *objects: Document | DocumentVersion | DocumentCollectionItem) -> None:
        self._s.add_all(objects)

    async def get(
        self, workspace_id: uuid.UUID, document_id: uuid.UUID, *, for_update: bool = False
    ) -> Document | None:
        stmt = select(Document).where(
            Document.workspace_id == workspace_id, Document.id == document_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def by_checksum(self, workspace_id: uuid.UUID, checksum: str) -> Document | None:
        stmt = select(Document).where(
            Document.workspace_id == workspace_id, Document.checksum_sha256 == checksum
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    def _filtered(self, workspace_id: uuid.UUID, f: DocumentFilter) -> Select[Document]:
        stmt = select(Document).where(Document.workspace_id == workspace_id)
        if f.archived == "exclude":
            stmt = stmt.where(Document.archived_at.is_(None))
        elif f.archived == "only":
            stmt = stmt.where(Document.archived_at.is_not(None))
        if f.query:
            pattern = _like_pattern(f.query.strip())
            stmt = stmt.where(
                or_(
                    Document.title.ilike(pattern, escape="\\"),
                    Document.original_filename.ilike(pattern, escape="\\"),
                )
            )
        if f.status:
            stmt = stmt.where(Document.status == f.status)
        if f.format:
            stmt = stmt.where(Document.format == f.format)
        if f.uploaded_by:
            stmt = stmt.where(Document.uploaded_by_user_id == f.uploaded_by)
        if f.collection_id:
            stmt = stmt.join(
                DocumentCollectionItem,
                (DocumentCollectionItem.document_id == Document.id)
                & (DocumentCollectionItem.workspace_id == workspace_id),
            ).where(DocumentCollectionItem.collection_id == f.collection_id)
        return stmt

    async def search(
        self, workspace_id: uuid.UUID, f: DocumentFilter
    ) -> tuple[list[Document], int]:
        base = self._filtered(workspace_id, f)
        total = (
            await self._s.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        column = getattr(Document, f.sort)
        order = column.desc() if f.descending else column.asc()
        rows = await self._s.execute(
            base.order_by(order, Document.id).limit(f.limit).offset(f.offset)
        )
        return list(rows.scalars()), int(total)

    async def versions(
        self, workspace_id: uuid.UUID, document_id: uuid.UUID
    ) -> list[DocumentVersion]:
        stmt = (
            select(DocumentVersion)
            .where(
                DocumentVersion.workspace_id == workspace_id,
                DocumentVersion.document_id == document_id,
            )
            .order_by(DocumentVersion.version_number.desc())
        )
        return list((await self._s.execute(stmt)).scalars())

    async def get_version(
        self, workspace_id: uuid.UUID, document_id: uuid.UUID, version_id: uuid.UUID
    ) -> DocumentVersion | None:
        stmt = select(DocumentVersion).where(
            DocumentVersion.workspace_id == workspace_id,
            DocumentVersion.document_id == document_id,
            DocumentVersion.id == version_id,
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def collection_ids(
        self, workspace_id: uuid.UUID, document_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[uuid.UUID]]:
        if not document_ids:
            return {}
        stmt = select(
            DocumentCollectionItem.document_id, DocumentCollectionItem.collection_id
        ).where(
            DocumentCollectionItem.workspace_id == workspace_id,
            DocumentCollectionItem.document_id.in_(document_ids),
        )
        result: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for document_id, collection_id in await self._s.execute(stmt):
            result[document_id].append(collection_id)
        return result

    async def delete(self, document: Document) -> None:
        await self._s.delete(document)


class CollectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    def add(self, collection: DocumentCollection) -> None:
        self._s.add(collection)

    async def get(
        self, workspace_id: uuid.UUID, collection_id: uuid.UUID
    ) -> DocumentCollection | None:
        stmt = select(DocumentCollection).where(
            DocumentCollection.workspace_id == workspace_id,
            DocumentCollection.id == collection_id,
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def name_taken(
        self, workspace_id: uuid.UUID, name: str, *, exclude: uuid.UUID | None = None
    ) -> bool:
        stmt = select(DocumentCollection.id).where(
            DocumentCollection.workspace_id == workspace_id,
            func.lower(DocumentCollection.name) == name.lower(),
        )
        if exclude is not None:
            stmt = stmt.where(DocumentCollection.id != exclude)
        return (await self._s.execute(stmt)).first() is not None

    async def list_with_counts(
        self, workspace_id: uuid.UUID
    ) -> list[tuple[DocumentCollection, int]]:
        count = (
            select(func.count())
            .where(
                DocumentCollectionItem.collection_id == DocumentCollection.id,
                DocumentCollectionItem.workspace_id == workspace_id,
            )
            .correlate(DocumentCollection)
            .scalar_subquery()
        )
        stmt = (
            select(DocumentCollection, count)
            .where(DocumentCollection.workspace_id == workspace_id)
            .order_by(func.lower(DocumentCollection.name))
        )
        return [(c, int(n)) for c, n in await self._s.execute(stmt)]

    async def count_documents(self, workspace_id: uuid.UUID, collection_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(
            DocumentCollectionItem.workspace_id == workspace_id,
            DocumentCollectionItem.collection_id == collection_id,
        )
        return int((await self._s.execute(stmt)).scalar_one())

    async def add_document(
        self,
        workspace_id: uuid.UUID,
        collection_id: uuid.UUID,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> bool:
        """Idempotent. Returns True if the document was newly added."""
        stmt = (
            insert(DocumentCollectionItem)
            .values(
                workspace_id=workspace_id,
                collection_id=collection_id,
                document_id=document_id,
                added_by_user_id=user_id,
            )
            .on_conflict_do_nothing()
            .returning(DocumentCollectionItem.document_id)
        )
        return (await self._s.execute(stmt)).first() is not None

    async def remove_document(
        self, workspace_id: uuid.UUID, collection_id: uuid.UUID, document_id: uuid.UUID
    ) -> bool:
        stmt = (
            delete(DocumentCollectionItem)
            .where(
                DocumentCollectionItem.workspace_id == workspace_id,
                DocumentCollectionItem.collection_id == collection_id,
                DocumentCollectionItem.document_id == document_id,
            )
            .returning(DocumentCollectionItem.document_id)
        )
        return (await self._s.execute(stmt)).first() is not None

    async def delete(self, collection: DocumentCollection) -> None:
        await self._s.delete(collection)
