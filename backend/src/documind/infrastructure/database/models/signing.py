"""DocuMind Sign persistence (design: docs/documind-sign/02-technical-design.md §3).

Data-plane tables (listed in ``DATA_PLANE_TABLES``): agreements and everything that
belongs to them. Children reference their agreement through composite
``(agreement_id, workspace_id)`` foreign keys, and fields reference their recipient
through ``(recipient_id, agreement_id, workspace_id)``, so the database itself keeps a
field, recipient or signature from pointing into another agreement or workspace.

Documents are referenced by plain ids (``source_document_id``, ``signed_document_id``):
deleting or archiving a document must never cascade into signing evidence, which keeps
its own files and hashes.

``SigningToken`` is the one control-plane table: it maps a hashed link or session token
to the workspace, agreement and recipient, so the public portal can find the right data
plane. It holds no titles, names, emails or document content.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
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

AGREEMENT_STATUSES = (
    "draft",
    "sent",
    "partially_signed",
    "finalizing",
    "completed",
    "declined",
    "voided",
    "expired",
    "finalization_failed",
)
RECIPIENT_STATUSES = (
    "pending",
    "invited",
    "viewed",
    "consented",
    "signed",
    "approved",
    "declined",
    "cancelled",
    "expired",
)
FIELD_TYPES = ("signature", "initials", "full_name", "date_signed", "text", "checkbox", "company")
FILE_KINDS = ("snapshot", "final", "certificate", "signature")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _agreement_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["agreement_id", "workspace_id"],
        ["agreements.id", "agreements.workspace_id"],
        ondelete="CASCADE",
        name=f"fk_{table}_agreement_workspace",
    )


class Agreement(Base):
    __tablename__ = "agreements"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_agreements_id_workspace_id"),
        CheckConstraint(_in("status", AGREEMENT_STATUSES), name="status_valid"),
        CheckConstraint(_in("routing", ("sequential", "parallel", "groups")), name="routing_valid"),
        Index("ix_agreements_workspace_id_status", "workspace_id", "status"),
        Index("ix_agreements_workspace_id_created_at", "workspace_id", "created_at"),
        Index(
            "ix_agreements_workspace_id_created_by_user_id", "workspace_id", "created_by_user_id"
        ),
        Index("ix_agreements_status_expires_at", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(300))
    message: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="draft", server_default="draft")
    routing: Mapped[str] = mapped_column(
        String(16), default="sequential", server_default="sequential"
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    #: The DocuMind document and version the agreement was prepared from (plain ids).
    source_document_id: Mapped[uuid.UUID | None] = mapped_column()
    source_version_id: Mapped[uuid.UUID | None] = mapped_column()
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    #: The restricted collection created for this agreement (C4), if any.
    collection_id: Mapped[uuid.UUID | None] = mapped_column()
    #: The DocuMind document created from the signed PDF.
    signed_document_id: Mapped[uuid.UUID | None] = mapped_column()
    expires_at: Mapped[datetime | None] = mapped_column(TZ)
    sent_at: Mapped[datetime | None] = mapped_column(TZ)
    completed_at: Mapped[datetime | None] = mapped_column(TZ)
    declined_at: Mapped[datetime | None] = mapped_column(TZ)
    voided_at: Mapped[datetime | None] = mapped_column(TZ)
    void_reason: Mapped[str | None] = mapped_column(Text)
    finalize_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    finalize_lease_until: Mapped[datetime | None] = mapped_column(TZ)
    finalize_error: Mapped[str | None] = mapped_column(String(300))
    #: Bumped on every draft edit; clients send it back (optimistic concurrency).
    lock_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class AgreementFile(Base):
    """An immutable file: the snapshot, the final PDF, the certificate or a signature image."""

    __tablename__ = "agreement_files"
    __table_args__ = (
        _agreement_fk("agreement_files"),
        UniqueConstraint("id", "workspace_id", name="uq_agreement_files_id_workspace_id"),
        CheckConstraint(_in("kind", FILE_KINDS), name="kind_valid"),
        # One snapshot, one final PDF and one certificate per agreement.
        Index(
            "uq_agreement_files_agreement_id_kind",
            "agreement_id",
            "kind",
            unique=True,
            postgresql_where=sql_text("kind IN ('snapshot', 'final', 'certificate')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agreement_id: Mapped[uuid.UUID] = mapped_column()
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    kind: Mapped[str] = mapped_column(String(16))
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    media_type: Mapped[str] = mapped_column(String(64))
    #: Snapshot only: ``[{"index", "cropbox": [x0, y0, x1, y1], "rotate"}]``.
    page_geometry: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class AgreementRecipient(Base):
    __tablename__ = "agreement_recipients"
    __table_args__ = (
        _agreement_fk("agreement_recipients"),
        UniqueConstraint(
            "id", "agreement_id", "workspace_id", name="uq_agreement_recipients_id_agreement"
        ),
        UniqueConstraint("agreement_id", "email", name="uq_agreement_recipients_agreement_email"),
        CheckConstraint(_in("status", RECIPIENT_STATUSES), name="status_valid"),
        CheckConstraint(_in("role", ("signer", "approver")), name="role_valid"),
        CheckConstraint("group_order >= 1", name="group_order_positive"),
        Index("ix_agreement_recipients_workspace_id_email", "workspace_id", "email"),
        Index("ix_agreement_recipients_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agreement_id: Mapped[uuid.UUID] = mapped_column()
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))  # stored lower-case
    role: Mapped[str] = mapped_column(String(16), default="signer", server_default="signer")
    group_order: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    auth_method: Mapped[str] = mapped_column(String(32), default="link", server_default="link")
    #: The workspace member this recipient is, matched by verified email at send (C3).
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    consent_version: Mapped[str | None] = mapped_column(String(32))
    invited_at: Mapped[datetime | None] = mapped_column(TZ)
    viewed_at: Mapped[datetime | None] = mapped_column(TZ)
    consented_at: Mapped[datetime | None] = mapped_column(TZ)
    completed_at: Mapped[datetime | None] = mapped_column(TZ)
    declined_at: Mapped[datetime | None] = mapped_column(TZ)
    decline_reason: Mapped[str | None] = mapped_column(Text)
    reminders_sent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_reminded_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class AgreementField(Base):
    """A place on the page a recipient must fill. Coordinates: domain/signing/fields.py."""

    __tablename__ = "agreement_fields"
    __table_args__ = (
        _agreement_fk("agreement_fields"),
        ForeignKeyConstraint(
            ["recipient_id", "agreement_id", "workspace_id"],
            [
                "agreement_recipients.id",
                "agreement_recipients.agreement_id",
                "agreement_recipients.workspace_id",
            ],
            ondelete="CASCADE",
            name="fk_agreement_fields_recipient",
        ),
        UniqueConstraint(
            "id", "agreement_id", "workspace_id", name="uq_agreement_fields_id_agreement"
        ),
        CheckConstraint(_in("type", FIELD_TYPES), name="type_valid"),
        CheckConstraint("width > 0 AND height > 0", name="size_positive"),
        CheckConstraint("page_rotation IN (0, 90, 180, 270)", name="rotation_valid"),
        Index("ix_agreement_fields_agreement_id_recipient_id", "agreement_id", "recipient_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agreement_id: Mapped[uuid.UUID] = mapped_column()
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    recipient_id: Mapped[uuid.UUID] = mapped_column()
    type: Mapped[str] = mapped_column(String(16))
    page_index: Mapped[int] = mapped_column(Integer)
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    width: Mapped[float] = mapped_column(Float)
    height: Mapped[float] = mapped_column(Float)
    page_width: Mapped[float] = mapped_column(Float)
    page_height: Mapped[float] = mapped_column(Float)
    page_rotation: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    required: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sql_text("true"))
    label: Mapped[str | None] = mapped_column(String(200))
    rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    #: Written once, by the recipient (or the server for name and date fields).
    value: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class AgreementSignature(Base):
    """A signature or initials mark. One per field: it can never be replaced."""

    __tablename__ = "agreement_signatures"
    __table_args__ = (
        _agreement_fk("agreement_signatures"),
        ForeignKeyConstraint(
            ["field_id", "agreement_id", "workspace_id"],
            [
                "agreement_fields.id",
                "agreement_fields.agreement_id",
                "agreement_fields.workspace_id",
            ],
            ondelete="CASCADE",
            name="fk_agreement_signatures_field",
        ),
        ForeignKeyConstraint(
            ["file_id", "workspace_id"],
            ["agreement_files.id", "agreement_files.workspace_id"],
            ondelete="CASCADE",
            name="fk_agreement_signatures_file",
        ),
        CheckConstraint(_in("method", ("drawn", "typed", "uploaded")), name="method_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agreement_id: Mapped[uuid.UUID] = mapped_column()
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    recipient_id: Mapped[uuid.UUID] = mapped_column()
    field_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    method: Mapped[str] = mapped_column(String(16))
    file_id: Mapped[uuid.UUID] = mapped_column()
    typed_text: Mapped[str | None] = mapped_column(String(200))
    font: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class AgreementEvent(Base):
    """Append-only evidence. A database trigger refuses UPDATE; ``hash`` chains the events."""

    __tablename__ = "agreement_events"
    __table_args__ = (
        _agreement_fk("agreement_events"),
        UniqueConstraint("agreement_id", "seq", name="uq_agreement_events_agreement_id_seq"),
        CheckConstraint(
            _in("actor_type", ("user", "recipient", "system")), name="actor_type_valid"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agreement_id: Mapped[uuid.UUID] = mapped_column()
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(48))
    actor_type: Mapped[str] = mapped_column(String(16))
    #: Plain ids: evidence survives the user's or recipient's removal.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column()
    recipient_id: Mapped[uuid.UUID | None] = mapped_column()
    occurred_at: Mapped[datetime] = mapped_column(TZ)
    request_id: Mapped[str | None] = mapped_column(String(128))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class AgreementNotification(Base):
    """An email to send. The unique key makes a repeated job a no-op."""

    __tablename__ = "agreement_notifications"
    __table_args__ = (
        _agreement_fk("agreement_notifications"),
        CheckConstraint(_in("status", ("pending", "sent", "failed")), name="status_valid"),
        Index("ix_agreement_notifications_status_next_attempt_at", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agreement_id: Mapped[uuid.UUID] = mapped_column()
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    recipient_id: Mapped[uuid.UUID | None] = mapped_column()
    kind: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    to_email: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(12), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(300))
    next_attempt_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class SigningToken(Base):
    """Control plane: hashed link and session tokens for the public signing portal."""

    __tablename__ = "signing_tokens"
    __table_args__ = (
        CheckConstraint(_in("kind", ("invite", "session")), name="kind_valid"),
        Index("ix_signing_tokens_agreement_id", "agreement_id"),
        Index("ix_signing_tokens_recipient_id", "recipient_id"),
        Index("ix_signing_tokens_expires_at", "expires_at"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    agreement_id: Mapped[uuid.UUID] = mapped_column()
    recipient_id: Mapped[uuid.UUID] = mapped_column()
    #: How the session was opened: ``link`` or ``documind_session`` (C3).
    via: Mapped[str] = mapped_column(String(32), default="link", server_default="link")
    expires_at: Mapped[datetime] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)
    used_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
