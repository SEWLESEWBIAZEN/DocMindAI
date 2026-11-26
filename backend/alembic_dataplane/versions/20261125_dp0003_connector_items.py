"""Synced-file mapping for connectors (ADR 0035).

Schema only: one new table. No existing rows change.

Revision ID: dp0003
Revises: dp0002
Create Date: 2026-11-25 16:54:17.750133+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "dp0003"
down_revision: str | None = "dp0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
            ["tenant_workspaces.workspace_id"],
            name=op.f("fk_connector_items_workspace_id_tenant_workspaces"),
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
    op.drop_index("ix_connector_items_document_id", table_name="connector_items")
    op.drop_table("connector_items")
