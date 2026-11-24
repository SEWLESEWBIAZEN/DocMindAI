"""Retrieval and answer value objects."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    chunk_index: int
    text: str
    page_start: int | None
    page_end: int | None
    heading_path: tuple[str, ...]
    # Fused relevance (higher is better) and its components.
    score: float
    vector_similarity: float | None = None
    keyword_rank: float | None = None


@dataclass(frozen=True, slots=True)
class ContextSource:
    number: int
    chunk: RetrievedChunk


@dataclass(frozen=True, slots=True)
class BuiltContext:
    sources: tuple[ContextSource, ...]
    text: str
    token_count: int
    dropped: int  # retrieved chunks left out (budget or near-duplicates)


@dataclass(frozen=True, slots=True)
class Citation:
    number: int
    document_id: uuid.UUID
    document_name: str
    chunk_id: uuid.UUID
    page_start: int | None
    page_end: int | None
    heading_path: tuple[str, ...]
    excerpt: str
    # "Halcyon MSA, p. 14" for display; "Halcyon MSA, page 14" for speech (ADR 0011).
    label: str
    spoken_label: str


@dataclass(frozen=True, slots=True)
class Turn:
    """One earlier exchange of a conversation: a question and its completed answer."""

    question: str
    answer: str
