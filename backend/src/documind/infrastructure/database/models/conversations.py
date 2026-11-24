"""Conversation persistence: conversations, their selected sources, messages, citations.

Like documents, children reference their parents through composite
``(id, workspace_id)`` foreign keys, so a message, source selection or citation
can never point into another workspace.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.infrastructure.database.base import Base

TZ = DateTime(timezone=True)
MESSAGE_STATUSES = ("streaming", "completed", "failed", "cancelled")


def _conversation_fk(name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["conversation_id", "workspace_id"],
        ["conversations.id", "conversations.workspace_id"],
        ondelete="CASCADE",
        name=name,
    )


class Conversation(Base):
    """A private, persistent chat owned by one member of a workspace."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_conversations_id_workspace_id"),
        CheckConstraint("title_source IN ('auto', 'user')", name="title_source_valid"),
        CheckConstraint("scope IN ('all', 'selected')", name="scope_valid"),
        Index(
            "ix_conversations_workspace_id_owner_last_message_at",
            "workspace_id",
            "created_by_user_id",
            "last_message_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    # Conversations are private to their creator and go with the account.
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(200))
    title_source: Mapped[str] = mapped_column(String(8), default="auto", server_default="auto")
    # "all": every ready document in the workspace; "selected": only the
    # documents and collections listed below (nothing, if they are all gone).
    scope: Mapped[str] = mapped_column(String(8), default="all", server_default="all")
    message_count: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    last_message_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class ConversationDocument(Base):
    __tablename__ = "conversation_documents"
    __table_args__ = (
        _conversation_fk("fk_conversation_documents_conversation_workspace"),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            ondelete="CASCADE",
            name="fk_conversation_documents_document_workspace",
        ),
        Index("ix_conversation_documents_document_id", "document_id"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    added_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class ConversationCollection(Base):
    __tablename__ = "conversation_collections"
    __table_args__ = (
        _conversation_fk("fk_conversation_collections_conversation_workspace"),
        ForeignKeyConstraint(
            ["collection_id", "workspace_id"],
            ["document_collections.id", "document_collections.workspace_id"],
            ondelete="CASCADE",
            name="fk_conversation_collections_collection_workspace",
        ),
        Index("ix_conversation_collections_collection_id", "collection_id"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    collection_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    added_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class Message(Base):
    """A user question or an assistant answer.

    Assistant messages start as ``streaming`` and end as ``completed``,
    ``failed`` or ``cancelled``; only ``completed`` ones carry verified
    citations and feed later turns. Regenerating marks the previous answer
    ``superseded_at`` and adds a new one replying to the same question.
    """

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_messages_id_workspace_id"),
        UniqueConstraint("conversation_id", "sequence"),
        _conversation_fk("fk_messages_conversation_workspace"),
        CheckConstraint("role IN ('user', 'assistant')", name="role_valid"),
        CheckConstraint(f"status IN {MESSAGE_STATUSES}", name="status_valid"),
        CheckConstraint(
            "answer_status IS NULL OR answer_status IN ('grounded', 'insufficient_context')",
            name="answer_status_valid",
        ),
        # At most one answer is being generated per conversation.
        Index(
            "uq_messages_streaming_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=sql_text("status = 'streaming'"),
        ),
        Index("ix_messages_status_updated_at", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    conversation_id: Mapped[uuid.UUID] = mapped_column()
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(String(16))
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Answer metadata (assistant messages).
    answer_status: Mapped[str | None] = mapped_column(String(24))
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    retrieval_mode: Mapped[str | None] = mapped_column(String(16))
    retrieval_query: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    embedding_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    superseded_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(TZ)


class MessageCitation(Base):
    """A verified [n] citation of a completed answer, snapshotted at answer time.

    The document and chunk may later be deleted or re-processed; the snapshot
    (name, pages, excerpt, labels) keeps the answer readable, and the ids are
    set to NULL so nothing dangles.
    """

    __tablename__ = "message_citations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["message_id", "workspace_id"],
            ["messages.id", "messages.workspace_id"],
            ondelete="CASCADE",
            name="fk_message_citations_message_workspace",
        ),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            # PostgreSQL 15+: null only document_id, keep workspace_id.
            ondelete="SET NULL (document_id)",
            name="fk_message_citations_document_workspace",
        ),
        UniqueConstraint("message_id", "number"),
        Index("ix_message_citations_document_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    message_id: Mapped[uuid.UUID] = mapped_column()
    number: Mapped[int] = mapped_column(Integer)
    document_id: Mapped[uuid.UUID | None] = mapped_column()
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="SET NULL"), index=True
    )
    document_name: Mapped[str] = mapped_column(String(255))
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    heading_path: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    excerpt: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(String(400))
    spoken_label: Mapped[str] = mapped_column(String(400))
