"""Read models returned by the conversation service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from documind.infrastructure.database.models import (
    Conversation,
    Message,
    MessageCitation,
    MessageFeedback,
)


@dataclass(frozen=True, slots=True)
class CitationInfo:
    number: int
    document_id: uuid.UUID | None
    document_name: str
    chunk_id: uuid.UUID | None
    page_start: int | None
    page_end: int | None
    heading_path: tuple[str, ...]
    excerpt: str
    label: str
    spoken_label: str
    # False once the document is deleted or archived (the snapshot stays readable).
    document_available: bool


@dataclass(frozen=True, slots=True)
class FeedbackInfo:
    rating: str
    reasons: tuple[str, ...]
    comment: str | None
    shared: bool
    updated_at: datetime


def feedback_info(f: MessageFeedback) -> FeedbackInfo:
    return FeedbackInfo(
        rating=f.rating,
        reasons=tuple(f.reasons or ()),
        comment=f.comment,
        shared=f.shared,
        updated_at=f.updated_at,
    )


@dataclass(frozen=True, slots=True)
class MessageInfo:
    id: uuid.UUID
    conversation_id: uuid.UUID
    sequence: int
    role: str
    content: str
    status: str
    reply_to_id: uuid.UUID | None
    answer_status: str | None
    model: str | None
    prompt_version: str | None
    retrieval_mode: str | None
    input_tokens: int
    output_tokens: int
    embedding_tokens: int
    latency_ms: int | None
    warnings: tuple[dict[str, Any], ...]
    error_code: str | None
    superseded: bool
    created_at: datetime
    completed_at: datetime | None
    citations: tuple[CitationInfo, ...] = field(default_factory=tuple)
    # The asking member's own rating (ADR 0038), if any.
    feedback: FeedbackInfo | None = None


@dataclass(frozen=True, slots=True)
class SelectedDocument:
    id: uuid.UUID
    title: str
    status: str
    archived: bool


@dataclass(frozen=True, slots=True)
class SelectedCollection:
    id: uuid.UUID
    name: str


@dataclass(frozen=True, slots=True)
class ConversationInfo:
    id: uuid.UUID
    title: str
    title_source: str
    scope: str
    document_ids: tuple[uuid.UUID, ...]
    collection_ids: tuple[uuid.UUID, ...]
    message_count: int
    last_message_at: datetime
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationDetail:
    conversation: ConversationInfo
    documents: tuple[SelectedDocument, ...]
    collections: tuple[SelectedCollection, ...]


def citation_info(c: MessageCitation, available: bool) -> CitationInfo:
    return CitationInfo(
        number=c.number,
        document_id=c.document_id,
        document_name=c.document_name,
        chunk_id=c.chunk_id,
        page_start=c.page_start,
        page_end=c.page_end,
        heading_path=tuple(c.heading_path or ()),
        excerpt=c.excerpt,
        label=c.label,
        spoken_label=c.spoken_label,
        document_available=available,
    )


def message_info(
    m: Message,
    citations: tuple[CitationInfo, ...] = (),
    feedback: MessageFeedback | None = None,
) -> MessageInfo:
    return MessageInfo(
        id=m.id,
        conversation_id=m.conversation_id,
        sequence=m.sequence,
        role=m.role,
        content=m.content,
        status=m.status,
        reply_to_id=m.reply_to_id,
        answer_status=m.answer_status,
        model=m.model,
        prompt_version=m.prompt_version,
        retrieval_mode=m.retrieval_mode,
        input_tokens=m.input_tokens,
        output_tokens=m.output_tokens,
        embedding_tokens=m.embedding_tokens,
        latency_ms=m.latency_ms,
        warnings=tuple(m.warnings or ()),
        error_code=m.error_code,
        superseded=m.superseded_at is not None,
        created_at=m.created_at,
        completed_at=m.completed_at,
        citations=citations,
        feedback=feedback_info(feedback) if feedback is not None else None,
    )


def conversation_info(
    c: Conversation, document_ids: list[uuid.UUID], collection_ids: list[uuid.UUID]
) -> ConversationInfo:
    return ConversationInfo(
        id=c.id,
        title=c.title,
        title_source=c.title_source,
        scope=c.scope,
        document_ids=tuple(document_ids),
        collection_ids=tuple(collection_ids),
        message_count=c.message_count,
        last_message_at=c.last_message_at,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )
