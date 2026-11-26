"""Per-collection access (ADR 0036).

Schema only: collection_members, and document_collections.restricted (default
false: every existing collection stays open). No existing rows change.

Revision ID: dp0004
Revises: dp0003
Create Date: 2026-11-26 04:46:08.413648+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "dp0004"
down_revision: str | None = "dp0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection_members",
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["collection_id", "workspace_id"],
            ["document_collections.id", "document_collections.workspace_id"],
            name="fk_collection_members_collection_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("collection_id", "user_id", name=op.f("pk_collection_members")),
    )
    op.create_index(
        "ix_collection_members_user_id", "collection_members", ["user_id"], unique=False
    )
    op.add_column(
        "document_collections",
        sa.Column("restricted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("document_collections", "restricted")
    op.drop_index("ix_collection_members_user_id", table_name="collection_members")
    op.drop_table("collection_members")
