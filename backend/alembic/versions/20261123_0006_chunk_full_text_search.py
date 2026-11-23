"""Full-text search over chunks for hybrid (vector + keyword) retrieval.

Adds a stored generated ``search_tsv`` column (English configuration) and a
GIN index. Postgres keeps it in sync with ``text`` automatically, so the
pipeline needs no changes and existing rows are backfilled by the ALTER.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-23 23:10:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_chunks",
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english'::regconfig, text)", persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_document_chunks_search_tsv",
        "document_chunks",
        ["search_tsv"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_search_tsv", table_name="document_chunks")
    op.drop_column("document_chunks", "search_tsv")
