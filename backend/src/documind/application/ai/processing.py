"""Per-workspace AI for background processing (worker side of ADR 0019/0020)."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.ai.resolver import AIResolver
from documind.application.billing.service import BillingService
from documind.application.processing.embedding_service import EmbeddingCache, EmbeddingService
from documind.application.processing.figures import FigureDescriber
from documind.domain.billing.plans import Capability


class ResolvedProcessingAI:
    """Embeddings and figure descriptions with the workspace's own key or the platform's.

    Figure descriptions billed to the platform are capped by the plan's remaining
    monthly quota; embeddings for an accepted upload always run (ADR 0020).
    """

    def __init__(
        self,
        resolver: AIResolver,
        control_sessions: async_sessionmaker[AsyncSession],
        cache_factory: Callable[[async_sessionmaker[AsyncSession]], EmbeddingCache],
        *,
        batch_size: int,
    ) -> None:
        self._resolver = resolver
        self._control = control_sessions
        self._cache_factory = cache_factory
        self._batch_size = batch_size

    async def for_workspace(
        self, workspace_id: uuid.UUID, data_sessions: async_sessionmaker[AsyncSession]
    ) -> tuple[EmbeddingService, FigureDescriber]:
        """``data_sessions``: the workspace's data plane (the embedding cache reads its chunks)."""
        ai = await self._resolver.resolve(workspace_id)
        limit: int | None = None
        if ai.vision is not None and ai.uses_platform(Capability.VISION):
            async with self._control() as session:
                limit = await BillingService(session).remaining_ai(
                    workspace_id, "figure_descriptions", ai.entitlements
                )
        return (
            EmbeddingService(
                ai.embeddings, self._cache_factory(data_sessions), batch_size=self._batch_size
            ),
            FigureDescriber(ai.vision, limit=limit),
        )
