"""Semantic and hybrid retrieval over a workspace's indexed documents."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.ports import EmbeddingProvider
from documind.domain.errors import NotFoundError, ServiceUnavailableError, ValidationFailedError
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext
from documind.domain.processing.errors import ProcessingError, ProviderAccessError
from documind.domain.rag.fusion import reciprocal_rank_fusion
from documind.domain.rag.models import RetrievedChunk
from documind.infrastructure.database.repositories.retrieval import (
    RetrievalRepository,
    SearchScope,
)

MAX_QUERY_CHARS = 2000

RetrievalMode = Literal["vector", "hybrid"]


@dataclass(frozen=True, slots=True)
class RetrievalOptions:
    top_k: int
    candidates: int
    min_similarity: float
    hybrid: bool
    ef_search: int


@dataclass(frozen=True, slots=True)
class SearchWarning:
    code: str
    message: str
    document_id: uuid.UUID | None = None
    count: int | None = None


REINDEX_WARNING = "documents_need_reindex"


def _names(titles: list[str], total: int) -> str:
    quoted = [f"'{t}'" for t in titles]
    more = total - len(quoted)
    if more > 0:
        return f"{', '.join(quoted)} and {more} more"
    if len(quoted) == 1:
        return quoted[0]
    return f"{', '.join(quoted[:-1])} and {quoted[-1]}"


def reindex_warning(count: int, titles: list[str], *, mode: RetrievalMode) -> SearchWarning:
    """Documents indexed with another embedding model are invisible to vector search."""
    one = count == 1
    subject = (
        f"{count} document was" if one else f"{count} documents were"
    ) + f" indexed with a different AI model ({_names(titles, count)})"
    they, them = ("it", "it") if one else ("they", "them")
    were = "was" if one else "were"
    if mode == "hybrid":
        effect = f"so {they} {were} matched only by exact words, not by meaning."
    else:
        effect = f"so {they} {were} not searched."
    return SearchWarning(
        REINDEX_WARNING,
        f"{subject}, {effect} Re-index {them} to include {them} fully.",
        count=count,
    )


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    chunks: list[RetrievedChunk]
    warnings: list[SearchWarning]
    mode: RetrievalMode
    embedding_model: str
    query_tokens: int


def fuse(
    vector_hits: Sequence[RetrievedChunk],
    keyword_hits: Sequence[RetrievedChunk],
    *,
    top_k: int,
) -> list[RetrievedChunk]:
    """Reciprocal-rank fusion; keeps per-retriever signals on the merged chunk."""
    by_id: dict[uuid.UUID, RetrievedChunk] = {}
    for hit in vector_hits:
        by_id[hit.chunk_id] = hit
    for hit in keyword_hits:
        existing = by_id.get(hit.chunk_id)
        by_id[hit.chunk_id] = replace(existing, keyword_rank=hit.keyword_rank) if existing else hit
    scores = reciprocal_rank_fusion(
        [[h.chunk_id for h in vector_hits], [h.chunk_id for h in keyword_hits]]
    )
    ranked = sorted(scores, key=lambda cid: (-scores[cid], str(cid)))[:top_k]
    return [replace(by_id[cid], score=scores[cid]) for cid in ranked]


class RetrievalService:
    def __init__(
        self, session: AsyncSession, embedder: EmbeddingProvider, options: RetrievalOptions
    ) -> None:
        self._repo = RetrievalRepository(session)
        self._embedder = embedder
        self._options = options

    @property
    def repository(self) -> RetrievalRepository:
        return self._repo

    async def _scope(
        self,
        ctx: WorkspaceContext,
        document_ids: Sequence[uuid.UUID] | None,
        collection_ids: Sequence[uuid.UUID] | None,
    ) -> tuple[SearchScope, list[SearchWarning]]:
        """``None`` = not restricted; a (possibly empty) list = restrict to it."""
        warnings: list[SearchWarning] = []
        docs: frozenset[uuid.UUID] | None = None
        cols: frozenset[uuid.UUID] | None = None
        if document_ids is not None:
            docs = frozenset(document_ids)
        if collection_ids is not None:
            cols = frozenset(collection_ids)
        if document_ids:
            wanted = set(document_ids)
            found = await self._repo.documents(ctx.workspace_id, wanted)
            if found.keys() != wanted:
                # Same response for "doesn't exist" and "another workspace's".
                raise NotFoundError("Document not found.")
            for doc in found.values():
                if doc.archived:
                    warnings.append(
                        SearchWarning(
                            "document_archived",
                            f"'{doc.title}' is archived and is not searched.",
                            doc.id,
                        )
                    )
                elif doc.status != "ready":
                    warnings.append(
                        SearchWarning(
                            "document_not_ready",
                            f"'{doc.title}' is not ready yet (status: {doc.status}).",
                            doc.id,
                        )
                    )
        if collection_ids:
            wanted = set(collection_ids)
            if await self._repo.collection_ids(ctx.workspace_id, wanted) != wanted:
                raise NotFoundError("Collection not found.")
        scope = SearchScope(ctx.workspace_id, docs, cols)
        if scope.empty:
            warnings.append(
                SearchWarning(
                    "selection_empty",
                    "None of the selected documents or collections are available any more.",
                )
            )
        return scope, warnings

    async def retrieve(
        self,
        ctx: WorkspaceContext,
        query: str,
        *,
        top_k: int | None = None,
        document_ids: Sequence[uuid.UUID] | None = None,
        collection_ids: Sequence[uuid.UUID] | None = None,
    ) -> RetrievalResult:
        ctx.require(Permission.DOCUMENTS_READ)
        query = " ".join(query.split())
        if not query:
            raise ValidationFailedError("Enter a question or search terms.", code="empty_query")
        if len(query) > MAX_QUERY_CHARS:
            raise ValidationFailedError(
                f"Queries are limited to {MAX_QUERY_CHARS} characters.", code="query_too_long"
            )
        opts = self._options
        k = top_k or opts.top_k
        scope, warnings = await self._scope(ctx, document_ids, collection_ids)
        if scope.empty:
            mode: RetrievalMode = "hybrid" if opts.hybrid else "vector"
            return RetrievalResult([], warnings, mode, self._embedder.model, 0)
        try:
            batch = await self._embedder.embed([query])
        except ProviderAccessError as exc:
            raise ServiceUnavailableError(exc.message, code=exc.code) from exc
        except ProcessingError as exc:
            raise ServiceUnavailableError(
                "Search is temporarily unavailable. Please try again.",
                code="embedding_unavailable",
            ) from exc
        candidates = max(opts.candidates, k)
        vector_hits = await self._repo.vector_search(
            scope,
            batch.vectors[0],
            embedding_model=self._embedder.model,
            limit=candidates,
            min_similarity=opts.min_similarity,
            ef_search=opts.ef_search,
        )
        if opts.hybrid:
            keyword_hits = await self._repo.keyword_search(scope, query, limit=candidates)
            chunks = fuse(vector_hits, keyword_hits, top_k=k)
            mode = "hybrid"
        else:
            chunks, mode = vector_hits[:k], "vector"
        stale, titles = await self._repo.stale_documents(scope, self._embedder.model)
        if stale:
            warnings.append(reindex_warning(stale, titles, mode=mode))
        return RetrievalResult(chunks, warnings, mode, self._embedder.model, batch.input_tokens)
