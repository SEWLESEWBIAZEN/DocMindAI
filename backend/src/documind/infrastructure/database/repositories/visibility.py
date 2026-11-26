"""Who may see which documents and collections (ADR 0036).

A collection can be *restricted*: only its members (and admins) see it. A
document in one or more restricted collections is visible only to members of at
least one of them; documents in no restricted collection are visible to everyone
in the workspace. The rule is applied in SQL wherever documents are read, so a
hidden document can't leak through search, answers, extraction or downloads.

``viewer`` is the user to filter for, or ``None`` for unrestricted access
(admins, and background work that was authorized when it started).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, or_, select, true

from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext
from documind.infrastructure.database.models import (
    CollectionMember,
    DocumentCollection,
    DocumentCollectionItem,
)

# Belongs to no collection: sees only documents that aren't restricted.
NOBODY = uuid.UUID(int=0)


def viewer_of(ctx: WorkspaceContext) -> uuid.UUID | None:
    """Admins and owners see every document; everyone else is filtered."""
    if ctx.public_only:
        return NOBODY
    return None if ctx.can(Permission.WORKSPACE_UPDATE) else ctx.user_id


def _member(collection_id: Any, viewer: uuid.UUID) -> ColumnElement[bool]:
    return exists().where(
        CollectionMember.collection_id == collection_id, CollectionMember.user_id == viewer
    )


def document_visible(document_id: Any, viewer: uuid.UUID | None) -> ColumnElement[bool]:
    if viewer is None:
        return true()
    in_restricted = (
        select(DocumentCollectionItem.collection_id)
        .join(
            DocumentCollection,
            and_(
                DocumentCollection.id == DocumentCollectionItem.collection_id,
                DocumentCollection.workspace_id == DocumentCollectionItem.workspace_id,
            ),
        )
        .where(
            DocumentCollectionItem.document_id == document_id,
            DocumentCollection.restricted.is_(True),
        )
    )
    allowed = in_restricted.where(_member(DocumentCollection.id, viewer))
    return or_(~in_restricted.exists(), allowed.exists())


def collection_visible(viewer: uuid.UUID | None) -> ColumnElement[bool]:
    if viewer is None:
        return true()
    return or_(DocumentCollection.restricted.is_(False), _member(DocumentCollection.id, viewer))
