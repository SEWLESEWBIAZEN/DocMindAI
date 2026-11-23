"""Documents, versions and collections.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-23 16:15:08.704921+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_collections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_document_collections_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_document_collections_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_collections")),
        sa.UniqueConstraint("id", "workspace_id", name="uq_document_collections_id_workspace_id"),
    )
    op.create_index(
        "uq_document_collections_workspace_id_name",
        "document_collections",
        ["workspace_id", sa.literal_column("lower(name)")],
        unique=True,
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("version_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=True),
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
            "format IN ('pdf', 'docx', 'txt', 'markdown')", name=op.f("ck_documents_format_valid")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed')",
            name=op.f("ck_documents_status_valid"),
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_documents_size_positive")),
        # FK to document_versions is added below (circular reference).
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"],
            ["users.id"],
            name=op.f("fk_documents_uploaded_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_documents_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("id", "workspace_id", name="uq_documents_id_workspace_id"),
    )
    op.create_index(
        "ix_documents_workspace_id_created_at",
        "documents",
        ["workspace_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_documents_workspace_id_status", "documents", ["workspace_id", "status"], unique=False
    )
    op.create_index(
        "uq_documents_workspace_id_checksum_sha256",
        "documents",
        ["workspace_id", "checksum_sha256"],
        unique=True,
    )
    op.create_table(
        "document_collection_items",
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("added_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["added_by_user_id"],
            ["users.id"],
            name=op.f("fk_document_collection_items_added_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["collection_id", "workspace_id"],
            ["document_collections.id", "document_collections.workspace_id"],
            name="fk_document_collection_items_collection_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_document_collection_items_document_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "collection_id", "document_id", name=op.f("pk_document_collection_items")
        ),
    )
    op.create_index(
        "ix_document_collection_items_document_id",
        "document_collection_items",
        ["document_id"],
        unique=False,
    )
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("scan_status", sa.String(length=16), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "format IN ('pdf', 'docx', 'txt', 'markdown')",
            name=op.f("ck_document_versions_format_valid"),
        ),
        sa.CheckConstraint(
            "scan_status IN ('clean', 'not_scanned')",
            name=op.f("ck_document_versions_scan_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_document_versions_document_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"],
            ["users.id"],
            name=op.f("fk_document_versions_uploaded_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_versions")),
        sa.UniqueConstraint(
            "document_id",
            "version_number",
            name=op.f("uq_document_versions_document_id_version_number"),
        ),
        sa.UniqueConstraint("storage_key", name=op.f("uq_document_versions_storage_key")),
    )

    op.create_foreign_key(
        op.f("fk_documents_current_version_id_document_versions"),
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_documents_current_version_id_document_versions"), "documents", type_="foreignkey"
    )
    op.drop_table("document_versions")
    op.drop_index(
        "ix_document_collection_items_document_id", table_name="document_collection_items"
    )
    op.drop_table("document_collection_items")
    op.drop_index("uq_documents_workspace_id_checksum_sha256", table_name="documents")
    op.drop_index("ix_documents_workspace_id_status", table_name="documents")
    op.drop_index("ix_documents_workspace_id_created_at", table_name="documents")
    op.drop_table("documents")
    op.drop_index("uq_document_collections_workspace_id_name", table_name="document_collections")
    op.drop_table("document_collections")
