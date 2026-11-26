"""Connectors: connections (control plane) and synced-file mapping (ADR 0035).

Schema only: two new tables, and the credentials kind check also allows
'connector_token'. No existing rows change.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-25 16:54:16.881263+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_KINDS_OLD = "kind IN ('ai_provider_key', 'database_password', 'storage_secret', 'worker_token')"
_KINDS_NEW = (
    "kind IN ('ai_provider_key', 'database_password', 'storage_secret', 'worker_token', "
    "'connector_token')"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_credentials_kind_valid"), "credentials", type_="check")
    op.create_check_constraint(op.f("ck_credentials_kind_valid"), "credentials", _KINDS_NEW)
    op.create_table(
        "connector_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("account_email", sa.String(length=320), nullable=True),
        sa.Column("credential_id", sa.Uuid(), nullable=True),
        sa.Column(
            "folders",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("collection_id", sa.Uuid(), nullable=True),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column(
            "known_folders",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("files_synced", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("files_skipped", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
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
            "provider IN ('google_drive')", name=op.f("ck_connector_connections_provider_valid")
        ),
        sa.CheckConstraint(
            "status IN ('needs_folders', 'active', 'error', 'paused')",
            name=op.f("ck_connector_connections_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_connector_connections_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["credentials.id"],
            name=op.f("fk_connector_connections_credential_id_credentials"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_connector_connections_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connector_connections")),
    )
    op.create_index(
        "ix_connector_connections_status_last_sync_at",
        "connector_connections",
        ["status", "last_sync_at"],
        unique=False,
    )
    op.create_index(
        "ix_connector_connections_workspace_id",
        "connector_connections",
        ["workspace_id"],
        unique=False,
    )
    op.create_table(
        "connector_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("mime_type", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=100), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_connector_items_document_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_connector_items_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connector_items")),
        sa.UniqueConstraint(
            "connection_id",
            "external_id",
            name=op.f("uq_connector_items_connection_id_external_id"),
        ),
    )
    op.create_index(
        "ix_connector_items_document_id", "connector_items", ["document_id"], unique=False
    )


def downgrade() -> None:
    op.execute("DELETE FROM credentials WHERE kind = 'connector_token'")
    op.drop_constraint(op.f("ck_credentials_kind_valid"), "credentials", type_="check")
    op.create_check_constraint(op.f("ck_credentials_kind_valid"), "credentials", _KINDS_OLD)
    op.drop_index("ix_connector_items_document_id", table_name="connector_items")
    op.drop_table("connector_items")
    op.drop_index("ix_connector_connections_workspace_id", table_name="connector_connections")
    op.drop_index(
        "ix_connector_connections_status_last_sync_at", table_name="connector_connections"
    )
    op.drop_table("connector_connections")
