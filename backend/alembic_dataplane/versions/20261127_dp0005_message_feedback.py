"""Answer feedback (ADR 0038), data plane.

Schema only: a new table message_feedback (a member's thumbs up/down on an
answer, with optional reasons, comment and consent to share it with admins).
No existing rows change.

Revision ID: dp0005
Revises: dp0004
Create Date: 2026-11-27 09:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "dp0005"
down_revision: str | None = "dp0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_feedback",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.String(length=4), nullable=False),
        sa.Column(
            "reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("shared", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "rating IN ('up', 'down')", name=op.f("ck_message_feedback_rating_valid")
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "workspace_id"],
            ["messages.id", "messages.workspace_id"],
            name="fk_message_feedback_message_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("message_id", name=op.f("pk_message_feedback")),
    )
    op.create_index(
        "ix_message_feedback_workspace_id_created_at",
        "message_feedback",
        ["workspace_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_message_feedback_user_id", "message_feedback", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_message_feedback_user_id", table_name="message_feedback")
    op.drop_index("ix_message_feedback_workspace_id_created_at", table_name="message_feedback")
    op.drop_table("message_feedback")
