"""Embeds chunks in batches, re-using vectors already computed for identical input."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from documind.application.ports import EmbeddingProvider
from documind.domain.processing.tokens import estimate_tokens


class EmbeddingCache(Protocol):
    async def find_embeddings(
        self, workspace_id: uuid.UUID, content_hashes: set[str], model: str, dimensions: int
    ) -> dict[str, list[float]]: ...


@dataclass(frozen=True, slots=True)
class EmbeddedTexts:
    vectors: list[list[float]]
    content_hashes: list[str]
    reused: int
    embedded: int
    input_tokens: int


def content_hash(model: str, dimensions: int, text: str) -> str:
    """Identity of an embedding: same model, dimensions and input => same vector."""
    return hashlib.sha256(f"{model}\x00{dimensions}\x00{text}".encode()).hexdigest()


class EmbeddingService:
    def __init__(
        self,
        provider: EmbeddingProvider,
        cache: EmbeddingCache,
        *,
        batch_size: int = 64,
        max_batch_tokens: int = 100_000,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._batch_size = batch_size
        self._max_batch_tokens = max_batch_tokens

    @property
    def model(self) -> str:
        return self._provider.model

    @property
    def dimensions(self) -> int:
        return self._provider.dimensions

    def _batches(self, texts: Sequence[str]) -> list[list[int]]:
        batches: list[list[int]] = []
        current: list[int] = []
        tokens = 0
        for i, text in enumerate(texts):
            size = estimate_tokens(text)
            if current and (
                len(current) >= self._batch_size or tokens + size > self._max_batch_tokens
            ):
                batches.append(current)
                current, tokens = [], 0
            current.append(i)
            tokens += size
        if current:
            batches.append(current)
        return batches

    async def embed(self, workspace_id: uuid.UUID, texts: Sequence[str]) -> EmbeddedTexts:
        model, dims = self._provider.model, self._provider.dimensions
        hashes = [content_hash(model, dims, t) for t in texts]
        known = await self._cache.find_embeddings(workspace_id, set(hashes), model, dims)
        reused = sum(1 for h in hashes if h in known)

        # Unique inputs that still need a vector, in first-seen order.
        pending: dict[str, str] = {}
        for h, text in zip(hashes, texts, strict=True):
            if h not in known and h not in pending:
                pending[h] = text
        pending_hashes = list(pending)
        pending_texts = list(pending.values())
        tokens = 0
        for batch in self._batches(pending_texts):
            result = await self._provider.embed([pending_texts[i] for i in batch])
            tokens += result.input_tokens
            for i, vector in zip(batch, result.vectors, strict=True):
                known[pending_hashes[i]] = vector

        return EmbeddedTexts(
            vectors=[known[h] for h in hashes],
            content_hashes=hashes,
            reused=reused,
            embedded=len(pending_hashes),
            input_tokens=tokens,
        )
