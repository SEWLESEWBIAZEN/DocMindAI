"""Approved infrastructure migrations against real PostgreSQL and MinIO (MI-8, ADR 0025).

A workspace with documents and a conversation moves from DocuMind's shared
database and bucket to the customer's own database and bucket.
"""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from documind.application.infrastructure.migration import (
    MigrationExecutor,
    MigrationFailedError,
    MigrationReport,
)
from documind.container import build_container
from tests.integration.conftest import Api
from tests.integration.test_infrastructure import (  # noqa: F401 - fixtures
    CUSTOMER_BUCKET,
    EXT_DB,
    _s3,
    create,
    customer_bucket,
    external_db,
    iapi,
    owner_org,
    plane_sql,
    ready,
    url,
)
from tests.integration.test_processing_pipeline import sql

pytestmark = pytest.mark.integration


def run_migration(api: Api, migration_id: str) -> MigrationReport:
    async def go() -> MigrationReport:
        c = build_container(api.settings)
        try:
            return await MigrationExecutor(
                c.session_factory, c.infrastructure(), drain_seconds=0
            ).run(uuid.UUID(migration_id))
        finally:
            await c.aclose()

    return asyncio.run(go())


def plan_migration(api: Api, w: str, elevated: dict[str, str], **body: Any) -> tuple[str, str]:
    vid = create(api, w, elevated, **body).json()["id"]
    validated = api.client.post(url(w, f"/versions/{vid}/validate"), headers=elevated).json()
    assert validated["status"] == "validated", validated["validation"]
    refused = api.client.post(url(w, f"/versions/{vid}/activate"), headers=elevated)
    assert refused.json()["code"] == "migration_required"
    (plan,) = api.client.get(url(w), headers=elevated).json()["migrations"]
    return vid, str(plan["id"])


def keys(bucket: str, api: Api) -> list[str]:
    listed = _s3(api.settings).list_objects_v2(Bucket=bucket)
    return [o["Key"] for o in listed.get("Contents", [])]


def test_approved_migration_moves_data_and_keeps_the_source_until_cleanup(
    iapi: Api, external_db: dict[str, Any], customer_bucket: dict[str, Any]
) -> None:
    api = iapi
    w, h, elevated = owner_org(api)
    ws = uuid.UUID(w)
    doc_id = ready(api, w, h)  # shared database and DocuMind's bucket
    conv = api.client.post(f"/api/v1/workspaces/{w}/conversations", json={}, headers=h).json()
    sent = api.client.post(
        f"/api/v1/workspaces/{w}/conversations/{conv['id']}/messages",
        json={"content": "How much notice?"},
        headers=h,
    )
    assert sent.status_code in (200, 201), sent.text
    platform_keys = [k for k in keys(api.settings.s3_bucket, api) if w in k]
    assert platform_keys

    vid, mid = plan_migration(api, w, elevated, database=external_db, storage=customer_bucket)

    # Approval is the customer's consent: owner with a fresh 2FA code.
    assert api.client.post(url(w, f"/migrations/{mid}/approve"), headers=h).status_code == 403
    queued: list[uuid.UUID] = []

    async def record(migration_id: uuid.UUID, **_: Any) -> bool:
        queued.append(migration_id)
        return True

    api.client.app.state.container.migration_dispatch = record  # type: ignore[attr-defined]
    approved = api.client.post(url(w, f"/migrations/{mid}/approve"), headers=elevated)
    assert approved.status_code == 202, approved.text
    assert approved.json() == {"id": mid, "status": "approved", "queued": True}
    assert queued == [uuid.UUID(mid)]
    again = api.client.post(url(w, f"/migrations/{mid}/approve"), headers=elevated)
    assert again.status_code == 404  # no longer planned

    # While a migration runs, the workspace's data is locked.
    sql(
        api,
        "UPDATE infrastructure_migrations SET status = 'running' WHERE id = :m",
        m=uuid.UUID(mid),
    )
    locked = api.client.get(f"/api/v1/workspaces/{w}/documents", headers=h)
    assert locked.status_code == 503
    assert locked.json()["code"] == "migration_in_progress"
    assert api.client.get(url(w), headers=h).status_code == 200  # settings stay reachable
    sql(
        api,
        "UPDATE infrastructure_migrations SET status = 'approved' WHERE id = :m",
        m=uuid.UUID(mid),
    )

    report = run_migration(api, mid)
    assert report.rows["documents"] == 1
    assert report.rows["messages"] == 2
    assert report.rows["document_chunks"] > 0
    assert report.objects == len(platform_keys)

    overview = api.client.get(url(w), headers=h).json()
    assert overview["active_version_id"] == vid
    assert {m["id"]: m["status"] for m in overview["migrations"]}[mid] == "completed"
    assert {e["kind"] for e in overview["events"]} >= {
        "migration_approved",
        "migration_started",
        "migration_completed",
    }

    # Everything is now served from the customer's database and bucket.
    assert plane_sql(api, EXT_DB, "SELECT count(*) AS n FROM documents")[0].n == 1
    assert plane_sql(api, EXT_DB, "SELECT count(*) AS n FROM messages")[0].n == 2
    listed = api.client.get(f"/api/v1/workspaces/{w}/documents", headers=h).json()
    assert [d["id"] for d in listed["items"]] == [doc_id]
    hits = api.client.post(
        f"/api/v1/workspaces/{w}/search", json={"query": "terminate notice"}, headers=h
    ).json()
    assert hits["results"]  # embeddings and the full-text index came across
    history = api.client.get(
        f"/api/v1/workspaces/{w}/conversations/{conv['id']}/messages", headers=h
    ).json()
    assert len(history["items"]) == 2
    signed = api.client.get(
        f"/api/v1/workspaces/{w}/documents/{doc_id}/download-url", headers=h
    ).json()["url"]
    assert CUSTOMER_BUCKET in signed
    assert len([k for k in keys(CUSTOMER_BUCKET, api) if w in k]) == len(platform_keys)

    # The source is retained until the owner explicitly deletes it.
    shared_docs = "SELECT count(*) AS n FROM documents WHERE workspace_id = :w"
    assert sql(api, shared_docs, w=ws)[0].n == 1
    assert all(k in keys(api.settings.s3_bucket, api) for k in platform_keys)
    assert api.client.post(url(w, f"/migrations/{mid}/cleanup"), headers=h).status_code == 403
    cleaned = api.client.post(url(w, f"/migrations/{mid}/cleanup"), headers=elevated)
    assert cleaned.status_code == 200, cleaned.text
    assert cleaned.json()["rows"]["documents"] == 1
    assert cleaned.json()["objects"] == len(platform_keys)
    assert sql(api, shared_docs, w=ws)[0].n == 0
    assert not [k for k in keys(api.settings.s3_bucket, api) if w in k]
    assert (
        api.client.post(url(w, f"/migrations/{mid}/cleanup"), headers=elevated).json()["code"]
        == "nothing_to_clean"
    )
    # The workspace keeps working at its new location.
    assert api.client.get(f"/api/v1/workspaces/{w}/documents", headers=h).json()["total"] == 1
    actions = {
        r.action for r in sql(api, "SELECT action FROM audit_logs WHERE workspace_id = :w", w=ws)
    }
    assert {
        "infrastructure.migration_approved",
        "infrastructure.migration_completed",
        "infrastructure.migration_source_cleaned",
    } <= actions


def test_failed_migration_leaves_the_source_untouched(
    iapi: Api, external_db: dict[str, Any]
) -> None:
    api = iapi
    w, h, elevated = owner_org(api)
    ws = uuid.UUID(w)
    ready(api, w, h)
    _, mid = plan_migration(api, w, elevated, database=external_db)
    # Someone else's rows for this workspace already sit in the destination.
    plane_sql(api, EXT_DB, "INSERT INTO tenant_workspaces (workspace_id) VALUES (:w)", w=ws)
    stray = uuid.uuid4()
    plane_sql(
        api,
        EXT_DB,
        "INSERT INTO document_collections (id, workspace_id, name) VALUES (:i, :w, 'stray')",
        i=stray,
        w=ws,
    )
    api.client.app.state.container.migration_dispatch = None  # type: ignore[attr-defined]
    approved = api.client.post(url(w, f"/migrations/{mid}/approve"), headers=elevated)
    assert approved.json()["queued"] is False  # run by an operator instead

    with pytest.raises(MigrationFailedError, match="already has"):
        run_migration(api, mid)

    overview = api.client.get(url(w), headers=h).json()
    assert overview["active_version_id"] is None  # still on the platform defaults
    failed = {m["id"]: m for m in overview["migrations"]}[mid]
    assert failed["status"] == "failed"
    assert "already has" in failed["plan"]["error"]
    # Unlocked; the source is intact; rows that were not ours were not deleted.
    assert api.client.get(f"/api/v1/workspaces/{w}/documents", headers=h).json()["total"] == 1
    assert plane_sql(api, EXT_DB, "SELECT id FROM document_collections")[0].id == stray
    assert plane_sql(api, EXT_DB, "SELECT count(*) AS n FROM documents")[0].n == 0


def test_failure_mid_copy_removes_the_partial_copy(
    iapi: Api,
    external_db: dict[str, Any],
    customer_bucket: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = iapi
    w, h, elevated = owner_org(api)
    ready(api, w, h)
    _, mid = plan_migration(api, w, elevated, database=external_db, storage=customer_bucket)
    api.client.app.state.container.migration_dispatch = None  # type: ignore[attr-defined]
    api.client.post(url(w, f"/migrations/{mid}/approve"), headers=elevated)

    copy_objects = MigrationExecutor._copy_objects

    async def copy_then_fail(self: MigrationExecutor, *args: Any) -> None:
        await copy_objects(self, *args)  # rows and objects are at the destination now
        raise RuntimeError("network lost")

    monkeypatch.setattr(MigrationExecutor, "_copy_objects", copy_then_fail)
    with pytest.raises(MigrationFailedError, match="network lost"):
        run_migration(api, mid)

    assert plane_sql(api, EXT_DB, "SELECT count(*) AS n FROM documents")[0].n == 0
    assert plane_sql(api, EXT_DB, "SELECT count(*) AS n FROM document_chunks")[0].n == 0
    assert not [k for k in keys(CUSTOMER_BUCKET, api) if w in k]
    assert api.client.get(f"/api/v1/workspaces/{w}/documents", headers=h).json()["total"] == 1
    hits = api.client.post(
        f"/api/v1/workspaces/{w}/search", json={"query": "terminate notice"}, headers=h
    ).json()
    assert hits["results"]  # still served from the source
