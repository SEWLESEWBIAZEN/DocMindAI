"""Tenant-scoped chunk retrieval: vector (pgvector HNSW) and keyword (Postgres FTS).

Every query applies the full visibility filter in SQL, so nothing outside the
caller's workspace (or archived, unfinished, superseded or embedded with a
different model) can ever be returned, even before Python sees a row:

- ``chunk.workspace_id`` and ``document.workspace_id`` both equal the workspace;
- the document is ``ready`` and not archived;
- the chunk belongs to the document's *current* version;
- the chunk was embedded with the configured model (vector search only);
- optional source selection: chunks of the selected documents *or* of
  documents in the selected collections (an empty selection matches nothing).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    Text,
    and_,
    case,
    cast,
    exists,
    false,
    func,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession

from documind.domain.rag.models import RetrievedChunk
from documind.infrastructure.database.models import (
    Document,
    DocumentChunk,
    DocumentCollection,
    DocumentCollectionItem,
)

_TS_CONFIG = "english"


@dataclass(frozen=True, slots=True)
class SearchScope:
    """``None`` for both means the whole workspace; otherwise the union of the two."""

    workspace_id: uuid.UUID
    document_ids: frozenset[uuid.UUID] | None = None
    collection_ids: frozenset[uuid.UUID] | None = None

    @property
    def restricted(self) -> bool:
        return self.document_ids is not None or self.collection_ids is not None

    @property
    def empty(self) -> bool:
        return self.restricted and not (self.document_ids or self.collection_ids)


@dataclass(frozen=True, slots=True)
class DocumentAvailability:
    id: uuid.UUID
    title: str
    status: str
    archived: bool


def _visible(scope: SearchScope) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [
        DocumentChunk.workspace_id == scope.workspace_id,
        Document.workspace_id == scope.workspace_id,
        Document.status == "ready",
        Document.archived_at.is_(None),
        DocumentChunk.document_version_id == Document.current_version_id,
    ]
    if scope.restricted:
        selected: list[ColumnElement[bool]] = []
        if scope.document_ids:
            selected.append(DocumentChunk.document_id.in_(scope.document_ids))
        if scope.collection_ids:
            selected.append(
                exists().where(
                    DocumentCollectionItem.workspace_id == scope.workspace_id,
                    DocumentCollectionItem.document_id == DocumentChunk.document_id,
                    DocumentCollectionItem.collection_id.in_(scope.collection_ids),
                )
            )
        conditions.append(or_(*selected) if selected else false())
    return conditions


def _base(*extra: Any) -> Select[Any]:
    return select(DocumentChunk, Document.title, *extra).join(
        Document,
        and_(
            Document.id == DocumentChunk.document_id,
            Document.workspace_id == DocumentChunk.workspace_id,
        ),
    )


def _to_domain(
    chunk: DocumentChunk,
    title: str,
    *,
    score: float,
    similarity: float | None = None,
    keyword_rank: float | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        document_title=title,
        chunk_index=chunk.chunk_index,
        text=chunk.text,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        heading_path=tuple(chunk.heading_path or ()),
        score=score,
        vector_similarity=similarity,
        keyword_rank=keyword_rank,
    )


class RetrievalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def vector_search(
        self,
        scope: SearchScope,
        vector: Sequence[float],
        *,
        embedding_model: str,
        limit: int,
        min_similarity: float,
        ef_search: int,
    ) -> list[RetrievedChunk]:
        """Nearest chunks by cosine similarity (1 - cosine distance), best first."""
        # Filtered HNSW scans can return fewer rows than LIMIT; iterative scans
        # (pgvector >= 0.8) keep scanning until enough rows pass the filters.
        # Transaction-local, so pooled connections are unaffected.
        await self._s.execute(
            select(
                func.set_config("hnsw.iterative_scan", "relaxed_order", True),
                func.set_config("hnsw.ef_search", str(max(ef_search, limit)), True),
            )
        )
        distance = DocumentChunk.embedding.cosine_distance(list(vector)).label("distance")
        stmt = (
            _base(distance)
            .where(*_visible(scope), DocumentChunk.embedding_model == embedding_model)
            .order_by(distance)
            .limit(limit)
        )
        hits: list[RetrievedChunk] = []
        for chunk, title, dist in (await self._s.execute(stmt)).all():
            similarity = 1.0 - float(dist)
            if similarity >= min_similarity:
                hits.append(_to_domain(chunk, title, score=similarity, similarity=similarity))
        # relaxed_order may return rows slightly out of order.
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits

    async def stale_documents(
        self, scope: SearchScope, embedding_model: str, *, sample: int = 3
    ) -> tuple[int, list[str]]:
        """Searchable documents in scope with chunks from another embedding model.

        Vector search can't see those chunks, so callers tell the user rather than
        answering as if the documents didn't exist. Returns the count and a few titles.
        """
        stale = (
            select(DocumentChunk.document_id)
            .join(
                Document,
                and_(
                    Document.id == DocumentChunk.document_id,
                    Document.workspace_id == DocumentChunk.workspace_id,
                ),
            )
            .where(*_visible(scope), DocumentChunk.embedding_model != embedding_model)
            .distinct()
            .subquery()
        )
        count = int((await self._s.execute(select(func.count()).select_from(stale))).scalar_one())
        if not count:
            return 0, []
        titles = (
            await self._s.execute(
                select(Document.title)
                .where(Document.id.in_(select(stale.c.document_id)))
                .order_by(Document.title)
                .limit(sample)
            )
        ).scalars()
        return count, list(titles)

    async def keyword_search(
        self, scope: SearchScope, query: str, *, limit: int
    ) -> list[RetrievedChunk]:
        """Full-text matches ranked by cover density (``ts_rank_cd``), best first.

        ``websearch_to_tsquery`` parses free text safely (quotes, ``or``, ``-``)
        and never raises on user input. Its AND between terms is relaxed to OR,
        because questions contain words a relevant passage need not repeat;
        passages matching more (and closer) terms still rank higher. Quoted
        phrases keep their phrase operator, and a query with an exclusion
        (``-term``) keeps strict AND so the exclusion still applies.
        """
        parsed = cast(func.websearch_to_tsquery(_TS_CONFIG, query), Text)
        relaxed = case(
            (func.strpos(parsed, "!") > 0, parsed),
            else_=func.replace(parsed, " & ", " | "),
        )
        tsquery = cast(relaxed, TSQUERY)
        rank = func.ts_rank_cd(DocumentChunk.search_tsv, tsquery).label("rank")
        stmt = (
            _base(rank)
            .where(*_visible(scope), DocumentChunk.search_tsv.op("@@")(tsquery))
            .order_by(rank.desc(), DocumentChunk.id)
            .limit(limit)
        )
        return [
            _to_domain(chunk, title, score=float(r), keyword_rank=float(r))
            for chunk, title, r in (await self._s.execute(stmt)).all()
        ]

    async def document_chunks(
        self,
        workspace_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        page_start: int | None,
        page_end: int | None,
        limit: int,
    ) -> list[RetrievedChunk]:
        """A document's current chunks in reading order, optionally limited to pages.

        Same visibility filter as search: ready, not archived, current version.
        """
        scope = SearchScope(workspace_id, frozenset({document_id}), None)
        stmt = _base().where(*_visible(scope))
        if page_start is not None:
            stmt = stmt.where(
                func.coalesce(DocumentChunk.page_end, DocumentChunk.page_start) >= page_start
            )
        if page_end is not None:
            stmt = stmt.where(DocumentChunk.page_start <= page_end)
        stmt = stmt.order_by(DocumentChunk.chunk_index).limit(limit)
        return [
            _to_domain(chunk, title, score=1.0)
            for chunk, title in (await self._s.execute(stmt)).all()
        ]

    async def documents(
        self, workspace_id: uuid.UUID, ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, DocumentAvailability]:
        stmt = select(Document.id, Document.title, Document.status, Document.archived_at).where(
            Document.workspace_id == workspace_id, Document.id.in_(ids)
        )
        return {
            row.id: DocumentAvailability(row.id, row.title, row.status, row.archived_at is not None)
            for row in (await self._s.execute(stmt)).all()
        }

    async def collection_ids(self, workspace_id: uuid.UUID, ids: set[uuid.UUID]) -> set[uuid.UUID]:
        stmt = select(DocumentCollection.id).where(
            DocumentCollection.workspace_id == workspace_id, DocumentCollection.id.in_(ids)
        )
        return set((await self._s.execute(stmt)).scalars())
