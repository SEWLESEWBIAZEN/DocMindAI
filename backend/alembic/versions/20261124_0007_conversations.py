"""Conversations: private chats, selected sources, messages and verified citations.

Composite (id, workspace_id) foreign keys keep every child row in its
conversation's workspace. A partial unique index allows at most one answer
streaming per conversation. Citations snapshot what they cite, and their
document reference is cleared (PostgreSQL 15+ column-list SET NULL) when the
document is deleted.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-24 00:07:19.839260+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("title_source", sa.String(length=8), server_default="auto", nullable=False),
        sa.Column("scope", sa.String(length=8), server_default="all", nullable=False),
        sa.Column("message_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope IN ('all', 'selected')", name=op.f("ck_conversations_scope_valid")
        ),
        sa.CheckConstraint(
            "title_source IN ('auto', 'user')", name=op.f("ck_conversations_title_source_valid")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_conversations_created_by_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_conversations_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.UniqueConstraint("id", "workspace_id", name="uq_conversations_id_workspace_id"),
    )
    op.create_index(
        "ix_conversations_workspace_id_owner_last_message_at",
        "conversations",
        ["workspace_id", "created_by_user_id", "last_message_at"],
        unique=False,
    )
    op.create_table(
        "conversation_collections",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["collection_id", "workspace_id"],
            ["document_collections.id", "document_collections.workspace_id"],
            name="fk_conversation_collections_collection_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "workspace_id"],
            ["conversations.id", "conversations.workspace_id"],
            name="fk_conversation_collections_conversation_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "conversation_id", "collection_id", name=op.f("pk_conversation_collections")
        ),
    )
    op.create_index(
        "ix_conversation_collections_collection_id",
        "conversation_collections",
        ["collection_id"],
        unique=False,
    )
    op.create_table(
        "conversation_documents",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "workspace_id"],
            ["conversations.id", "conversations.workspace_id"],
            name="fk_conversation_documents_conversation_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_conversation_documents_document_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "conversation_id", "document_id", name=op.f("pk_conversation_documents")
        ),
    )
    op.create_index(
        "ix_conversation_documents_document_id",
        "conversation_documents",
        ["document_id"],
        unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reply_to_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("answer_status", sa.String(length=24), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("retrieval_mode", sa.String(length=16), nullable=True),
        sa.Column("retrieval_query", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("embedding_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "answer_status IS NULL OR answer_status IN ('grounded', 'insufficient_context')",
            name=op.f("ck_messages_answer_status_valid"),
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f("ck_messages_role_valid")),
        sa.CheckConstraint(
            "status IN ('streaming', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_messages_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "workspace_id"],
            ["conversations.id", "conversations.workspace_id"],
            name="fk_messages_conversation_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_messages_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reply_to_id"],
            ["messages.id"],
            name=op.f("fk_messages_reply_to_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "conversation_id", "sequence", name=op.f("uq_messages_conversation_id_sequence")
        ),
        sa.UniqueConstraint("id", "workspace_id", name="uq_messages_id_workspace_id"),
    )
    op.create_index(
        "ix_messages_status_updated_at", "messages", ["status", "updated_at"], unique=False
    )
    op.create_index(
        "uq_messages_streaming_conversation",
        "messages",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'streaming'"),
    )
    op.create_table(
        "message_citations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("document_name", sa.String(length=255), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column(
            "heading_path",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("label", sa.String(length=400), nullable=False),
        sa.Column("spoken_label", sa.String(length=400), nullable=False),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["document_chunks.id"],
            name=op.f("fk_message_citations_chunk_id_document_chunks"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_message_citations_document_workspace",
            ondelete="SET NULL (document_id)",
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "workspace_id"],
            ["messages.id", "messages.workspace_id"],
            name="fk_message_citations_message_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_citations")),
        sa.UniqueConstraint(
            "message_id", "number", name=op.f("uq_message_citations_message_id_number")
        ),
    )
    op.create_index(
        op.f("ix_message_citations_chunk_id"), "message_citations", ["chunk_id"], unique=False
    )
    op.create_index(
        "ix_message_citations_document_id", "message_citations", ["document_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_message_citations_document_id", table_name="message_citations")
    op.drop_index(op.f("ix_message_citations_chunk_id"), table_name="message_citations")
    op.drop_table("message_citations")
    op.drop_index(
        "uq_messages_streaming_conversation",
        table_name="messages",
        postgresql_where=sa.text("status = 'streaming'"),
    )
    op.drop_index("ix_messages_status_updated_at", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversation_documents_document_id", table_name="conversation_documents")
    op.drop_table("conversation_documents")
    op.drop_index(
        "ix_conversation_collections_collection_id", table_name="conversation_collections"
    )
    op.drop_table("conversation_collections")
    op.drop_index("ix_conversations_workspace_id_owner_last_message_at", table_name="conversations")
    op.drop_table("conversations")
