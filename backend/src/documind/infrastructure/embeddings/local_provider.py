"""Deterministic local embeddings for development and tests. NOT for production.

Feature hashing of words and word bigrams into a fixed-size, L2-normalised
vector: texts sharing vocabulary get high cosine similarity, so the whole
pipeline (and later, retrieval) can run without an API key or network. It has
no semantic understanding; production requires a real provider (enforced by
configuration validation).
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re

from documind.application.ports import EmbeddingBatch
from documind.domain.processing.tokens import estimate_tokens

_WORD = re.compile(r"\w+", re.UNICODE)


class LocalHashingEmbeddingProvider:
    model = "local-hashing-v1"

    def __init__(self, dimensions: int) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _vector(self, text: str) -> list[float]:
        words = _WORD.findall(text.lower())
        features = words + [f"{a} {b}" for a, b in itertools.pairwise(words)]
        vector = [0.0] * self._dimensions
        for feature in features:
            digest = int.from_bytes(
                hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big"
            )
            vector[digest % self._dimensions] += -1.0 if digest >> 63 else 1.0
        norm = math.sqrt(sum(x * x for x in vector))
        if norm == 0:
            vector[0] = 1.0  # cosine distance is undefined for zero vectors
            return vector
        return [x / norm for x in vector]

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        return EmbeddingBatch(
            vectors=[self._vector(t) for t in texts],
            input_tokens=sum(estimate_tokens(t) for t in texts),
        )
