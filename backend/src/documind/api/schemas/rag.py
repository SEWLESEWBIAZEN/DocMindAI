"""Request and response bodies for search and grounded answers."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from documind.application.rag.retrieval import MAX_QUERY_CHARS

_MAX_FILTER_IDS = 100


class _Scoped(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_ids: list[uuid.UUID] | None = Field(
        default=None,
        max_length=_MAX_FILTER_IDS,
        description="Only search these documents (all must belong to the workspace).",
    )
    collection_ids: list[uuid.UUID] | None = Field(
        default=None,
        max_length=_MAX_FILTER_IDS,
        description="Only search documents in any of these collections.",
    )
    top_k: int | None = Field(default=None, ge=1, le=50)


class SearchRequest(_Scoped):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)


class AskRequest(_Scoped):
    question: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)


class WarningResponse(BaseModel):
    code: str
    message: str
    document_id: uuid.UUID | None = None


class SearchHitResponse(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    chunk_index: int
    text: str
    page_start: int | None
    page_end: int | None
    heading_path: list[str]
    score: float = Field(description="Fused rank score (hybrid) or cosine similarity (vector).")
    vector_similarity: float | None
    keyword_rank: float | None


class SearchResponse(BaseModel):
    query: str
    mode: Literal["vector", "hybrid"]
    embedding_model: str
    results: list[SearchHitResponse]
    warnings: list[WarningResponse]


class CitationResponse(BaseModel):
    number: int = Field(description="The [n] marker used in the answer text.")
    document_id: uuid.UUID
    document_name: str
    chunk_id: uuid.UUID
    page_start: int | None
    page_end: int | None
    heading_path: list[str]
    excerpt: str
    label: str = Field(description='Visual label, e.g. "MSA, p. 14".')
    spoken_label: str = Field(description='Screen-reader/voice label, e.g. "MSA, page 14".')


class UsageResponse(BaseModel):
    input_tokens: int
    output_tokens: int
    embedding_tokens: int


class AnswerResponse(BaseModel):
    answer: str
    status: Literal["grounded", "insufficient_context"]
    citations: list[CitationResponse]
    warnings: list[WarningResponse]
    model: str | None
    prompt_version: str
    retrieval_mode: Literal["vector", "hybrid"]
    sources_considered: int
    usage: UsageResponse
    latency_ms: int
