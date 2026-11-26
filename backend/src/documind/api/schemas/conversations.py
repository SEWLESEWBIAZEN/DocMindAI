"""Request and response bodies for conversations and messages."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from documind.application.conversations.feedback import (
    MAX_COMMENT,
    QualitySummary,
    SharedFeedback,
)
from documind.application.conversations.service import MAX_SOURCES, TITLE_MAX
from documind.application.conversations.views import (
    CitationInfo,
    ConversationDetail,
    ConversationInfo,
    FeedbackInfo,
    MessageInfo,
)
from documind.application.rag.retrieval import MAX_QUERY_CHARS


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourcesRequest(_Strict):
    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_SOURCES)
    collection_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=MAX_SOURCES,
        description=(
            "Answers use the selected documents plus every document in the selected "
            "collections. Leave both empty to use the whole workspace."
        ),
    )


class CreateConversationRequest(SourcesRequest):
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=TITLE_MAX,
        description="Optional; otherwise the first question becomes the title.",
    )


class RenameConversationRequest(_Strict):
    title: str = Field(min_length=1, max_length=TITLE_MAX)


class SendMessageRequest(_Strict):
    content: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)


class SelectedDocumentResponse(BaseModel):
    id: uuid.UUID
    title: str
    status: str
    archived: bool


class SelectedCollectionResponse(BaseModel):
    id: uuid.UUID
    name: str


class ConversationResponse(BaseModel):
    id: uuid.UUID
    title: str
    title_source: Literal["auto", "user"]
    scope: Literal["all", "selected"] = Field(
        description='"all": the whole workspace; "selected": only the selected sources.'
    )
    document_ids: list[uuid.UUID]
    collection_ids: list[uuid.UUID]
    message_count: int
    last_message_at: datetime
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, c: ConversationInfo) -> ConversationResponse:
        return cls(
            id=c.id,
            title=c.title,
            title_source=c.title_source,
            scope=c.scope,
            document_ids=list(c.document_ids),
            collection_ids=list(c.collection_ids),
            message_count=c.message_count,
            last_message_at=c.last_message_at,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )


class ConversationDetailResponse(ConversationResponse):
    documents: list[SelectedDocumentResponse]
    collections: list[SelectedCollectionResponse]

    @classmethod
    def of_detail(cls, d: ConversationDetail) -> ConversationDetailResponse:
        base = ConversationResponse.of(d.conversation).model_dump()
        return cls(
            **base,
            documents=[
                SelectedDocumentResponse(
                    id=x.id, title=x.title, status=x.status, archived=x.archived
                )
                for x in d.documents
            ],
            collections=[SelectedCollectionResponse(id=x.id, name=x.name) for x in d.collections],
        )


class ConversationPageResponse(BaseModel):
    items: list[ConversationResponse]
    total: int
    limit: int
    offset: int


class MessageCitationResponse(BaseModel):
    number: int = Field(description="The [n] marker used in the answer text.")
    document_id: uuid.UUID | None = Field(description="Null once the document was deleted.")
    document_name: str
    chunk_id: uuid.UUID | None
    page_start: int | None
    page_end: int | None
    heading_path: list[str]
    excerpt: str
    label: str
    spoken_label: str
    document_available: bool = Field(
        description="False when the document has since been deleted or archived."
    )

    @classmethod
    def of(cls, c: CitationInfo) -> MessageCitationResponse:
        return cls(
            number=c.number,
            document_id=c.document_id,
            document_name=c.document_name,
            chunk_id=c.chunk_id,
            page_start=c.page_start,
            page_end=c.page_end,
            heading_path=list(c.heading_path),
            excerpt=c.excerpt,
            label=c.label,
            spoken_label=c.spoken_label,
            document_available=c.document_available,
        )


class MessageUsageResponse(BaseModel):
    input_tokens: int
    output_tokens: int
    embedding_tokens: int


FeedbackReason = Literal[
    "incorrect", "incomplete", "bad_citation", "missed_document", "unclear", "other"
]


class MessageFeedbackRequest(_Strict):
    rating: Literal["up", "down"]
    reasons: list[FeedbackReason] = Field(
        default_factory=list,
        max_length=6,
        description="Why the answer was poor (thumbs down only; ignored for thumbs up).",
    )
    comment: str | None = Field(default=None, max_length=MAX_COMMENT)
    shared: bool = Field(
        default=False,
        description=(
            "Let workspace admins read this question and answer to improve answers. "
            "Without it they only see counts."
        ),
    )


class MessageFeedbackResponse(BaseModel):
    rating: Literal["up", "down"]
    reasons: list[str]
    comment: str | None
    shared: bool
    updated_at: datetime

    @classmethod
    def of(cls, f: FeedbackInfo) -> MessageFeedbackResponse:
        return cls(
            rating=f.rating,
            reasons=list(f.reasons),
            comment=f.comment,
            shared=f.shared,
            updated_at=f.updated_at,
        )


class MessageResponse(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    sequence: int
    role: Literal["user", "assistant"]
    content: str
    status: Literal["streaming", "completed", "failed", "cancelled"]
    reply_to_id: uuid.UUID | None
    answer_status: Literal["grounded", "insufficient_context"] | None
    citations: list[MessageCitationResponse]
    warnings: list[dict[str, Any]]
    error_code: str | None
    superseded: bool
    model: str | None
    prompt_version: str | None
    retrieval_mode: str | None
    usage: MessageUsageResponse
    latency_ms: int | None
    created_at: datetime
    completed_at: datetime | None
    feedback: MessageFeedbackResponse | None = Field(
        default=None, description="Your own rating of this answer."
    )

    @classmethod
    def of(cls, m: MessageInfo) -> MessageResponse:
        return cls(
            id=m.id,
            conversation_id=m.conversation_id,
            sequence=m.sequence,
            role=m.role,
            content=m.content,
            status=m.status,
            reply_to_id=m.reply_to_id,
            answer_status=m.answer_status,
            citations=[MessageCitationResponse.of(c) for c in m.citations],
            warnings=list(m.warnings),
            error_code=m.error_code,
            superseded=m.superseded,
            model=m.model,
            prompt_version=m.prompt_version,
            retrieval_mode=m.retrieval_mode,
            usage=MessageUsageResponse(
                input_tokens=m.input_tokens,
                output_tokens=m.output_tokens,
                embedding_tokens=m.embedding_tokens,
            ),
            latency_ms=m.latency_ms,
            created_at=m.created_at,
            completed_at=m.completed_at,
            feedback=MessageFeedbackResponse.of(m.feedback) if m.feedback else None,
        )


class MessagePageResponse(BaseModel):
    items: list[MessageResponse] = Field(description="Oldest first.")
    has_more: bool = Field(description="Older messages exist; pass `before` to load them.")
    next_before: int | None = Field(description="Value for `before` to load the previous page.")


class ReplyResponse(BaseModel):
    """Non-streaming reply: the question (if new) and the finished answer."""

    user_message: MessageResponse | None
    assistant_message: MessageResponse


class ModelRatingResponse(BaseModel):
    model: str
    up: int
    down: int


class SharedFeedbackResponse(BaseModel):
    message_id: uuid.UUID
    rating: Literal["up", "down"]
    reasons: list[str]
    comment: str | None
    question: str
    answer: str
    answer_status: str | None
    citation_count: int
    model: str | None
    prompt_version: str | None
    rated_at: datetime

    @classmethod
    def of(cls, f: SharedFeedback) -> SharedFeedbackResponse:
        return cls(
            message_id=f.message_id,
            rating=f.rating,
            reasons=list(f.reasons),
            comment=f.comment,
            question=f.question,
            answer=f.answer,
            answer_status=f.answer_status,
            citation_count=f.citation_count,
            model=f.model,
            prompt_version=f.prompt_version,
            rated_at=f.rated_at,
        )


class AnswerQualityResponse(BaseModel):
    days: int
    answers: int = Field(description="Completed answers in the period.")
    insufficient_context: int = Field(
        description="Answers that said the documents don't cover the question."
    )
    rated: int
    up: int
    down: int
    reasons: dict[str, int] = Field(description="How often each reason was given (thumbs down).")
    models: list[ModelRatingResponse]
    shared: list[SharedFeedbackResponse] = Field(
        description="Ratings members chose to share with admins, newest first (at most 50)."
    )

    @classmethod
    def of(cls, q: QualitySummary) -> AnswerQualityResponse:
        return cls(
            days=q.days,
            answers=q.answers,
            insufficient_context=q.insufficient_context,
            rated=q.rated,
            up=q.up,
            down=q.down,
            reasons=q.reasons,
            models=[ModelRatingResponse(model=m.model, up=m.up, down=m.down) for m in q.models],
            shared=[SharedFeedbackResponse.of(f) for f in q.shared],
        )
