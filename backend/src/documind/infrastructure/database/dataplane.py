"""Data-plane schema for dedicated and external databases (ADR 0021).

The data tables are the same ORM models as in the shared database. In a
dedicated or external database:

* references to ``workspaces`` point to a local ``tenant_workspaces`` anchor,
  so the database accepts rows only for workspaces registered to it;
* references to ``users`` become plain UUIDs (identity stays in the control plane);
* ``audit_outbox`` holds audit entries written in the same transaction as the
  change; they are relayed to the control plane's ``audit_logs``.

``dataplane_metadata()`` derives this schema from the models, the separate
Alembic environment (``alembic_dataplane``) migrates it, and a parity test
keeps both schemas identical apart from those references.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKeyConstraint, MetaData, String, Table, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from documind.infrastructure.database import models  # noqa: F401  (registers every table)
from documind.infrastructure.database.base import NAMING_CONVENTION, Base

#: Tables that live in a workspace's data plane, in dependency order.
DATA_PLANE_TABLES: tuple[str, ...] = (
    "documents",
    "document_versions",
    "document_collections",
    "document_collection_items",
    "collection_members",
    "processing_jobs",
    "document_pages",
    "document_chunks",
    "conversations",
    "conversation_documents",
    "conversation_collections",
    "messages",
    "message_citations",
    "message_feedback",
    "extraction_runs",
    "extraction_results",
    "deadlines",
    "connector_items",
    # DocuMind Sign (docs/documind-sign/02-technical-design.md §3.1)
    "agreements",
    "agreement_files",
    "agreement_recipients",
    "agreement_fields",
    "agreement_signatures",
    "agreement_events",
    "agreement_notifications",
)
CONTROL_PLANE_REFERENCES = {"workspaces", "users"}
DATAPLANE_SCHEMA = "documind"
DATAPLANE_VERSION_TABLE = "documind_dataplane_version"
TZ = DateTime(timezone=True)


class DataPlaneBase(DeclarativeBase):
    """Tables that exist only in dedicated and external data planes."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TenantWorkspace(DataPlaneBase):
    __tablename__ = "tenant_workspaces"

    workspace_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    registered_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class AuditOutbox(DataPlaneBase):
    __tablename__ = "audit_outbox"

    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    workspace_id: Mapped[uuid.UUID | None] = mapped_column()
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column()
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    request_id: Mapped[str | None] = mapped_column(String(128))


def _replace_control_references(table: Table) -> None:
    """Drop FKs to control-plane tables; workspace FKs point to the tenant anchor."""
    for fk in list(table.foreign_key_constraints):
        target = fk.elements[0].target_fullname.split(".")[0]
        if target not in CONTROL_PLANE_REFERENCES:
            continue
        columns = [c.name for c in fk.columns]
        ondelete = fk.ondelete
        table.constraints.discard(fk)
        for element in fk.elements:
            element.parent.foreign_keys.discard(element)
            table.foreign_keys.discard(element)
        if target == "workspaces":
            table.append_constraint(
                ForeignKeyConstraint(columns, ["tenant_workspaces.workspace_id"], ondelete=ondelete)
            )


def dataplane_metadata() -> MetaData:
    """A fresh MetaData with the data-plane schema (migrations and the parity test)."""
    md = MetaData(naming_convention=NAMING_CONVENTION)
    for table in DataPlaneBase.metadata.sorted_tables:
        table.to_metadata(md)
    for name in DATA_PLANE_TABLES:
        _replace_control_references(Base.metadata.tables[name].to_metadata(md))
    return md


__all__ = [
    "DATAPLANE_SCHEMA",
    "DATAPLANE_VERSION_TABLE",
    "DATA_PLANE_TABLES",
    "AuditOutbox",
    "DataPlaneBase",
    "TenantWorkspace",
    "dataplane_metadata",
]
