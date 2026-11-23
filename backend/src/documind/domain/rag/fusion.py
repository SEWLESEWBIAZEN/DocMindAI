"""Reciprocal-rank fusion of several ranked result lists."""

from __future__ import annotations

from collections.abc import Hashable, Sequence

DEFAULT_K = 60


def reciprocal_rank_fusion[T: Hashable](
    rankings: Sequence[Sequence[T]], *, k: int = DEFAULT_K
) -> dict[T, float]:
    """score(d) = sum over lists of 1 / (k + rank), rank starting at 1 (Cormack et al., 2009)."""
    scores: dict[T, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores
