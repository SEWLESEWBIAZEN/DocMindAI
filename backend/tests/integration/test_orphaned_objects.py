"""Orphaned-object reconciliation against real PostgreSQL and MinIO (F6/F7)."""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
import io
import uuid
from datetime import timedelta
from typing import Any

import pytest

from documind.container import build_container
from documind.ops.orphans import OrphanReconciler, OrphanReport
from tests.integration.conftest import Api
from tests.integration.test_infrastructure import (  # noqa: F401 - fixtures
    _s3,
    customer_bucket,
    external_db,
    iapi,
    owner_org,
    ready,
    url,
)
from tests.integration.test_infrastructure_migration import keys, plan_migration, run_migration

pytestmark = pytest.mark.integration


def reconcile(api: Api, *, min_age: timedelta = timedelta(0), delete: bool = False) -> OrphanReport:
    async def go() -> OrphanReport:
        c = build_container(api.settings, role="cli")
        try:
            return await OrphanReconciler(
                s3_client=c.s3_client,
                bucket=c.settings.s3_bucket,
                control=c.session_factory,
                resolver=c.infrastructure(),
            ).run(min_age=min_age, delete=delete)
        finally:
            await c.aclose()

    return asyncio.run(go())


def put(api: Api, key: str) -> None:
    _s3(api.settings).put_object(Bucket=api.settings.s3_bucket, Key=key, Body=io.BytesIO(b"x"))


def test_finds_and_deletes_only_unreferenced_objects(iapi: Api) -> None:
    api = iapi
    w, h, _ = owner_org(api)
    ready(api, w, h)
    (live,) = [k for k in keys(api.settings.s3_bucket, api) if w in k]

    # A delete that failed after the commit, an export whose row is gone, and something
    # DocuMind didn't write.
    lost = f"workspaces/{w}/documents/{uuid.uuid4()}/versions/{uuid.uuid4()}"
    gone_ws = f"workspaces/{uuid.uuid4()}/documents/{uuid.uuid4()}/versions/{uuid.uuid4()}"
    export = f"exports/{uuid.uuid4()}/{uuid.uuid4()}.zip"
    stranger = f"someone-else/{uuid.uuid4()}.txt"
    for key in (lost, gone_ws, export, stranger):
        put(api, key)

    # Too new to judge: an upload may still be committing.
    fresh = reconcile(api, min_age=timedelta(hours=24))
    assert {lost, gone_ws, export}.isdisjoint(k for k, _ in fresh.orphans)
    assert fresh.too_new >= 3

    # Report only: nothing changes.
    report = reconcile(api)
    found = {k for k, _ in report.orphans}
    assert {lost, gone_ws, export} <= found
    assert live not in found
    assert stranger in report.unknown
    assert stranger not in found
    assert report.deleted == 0
    assert {lost, gone_ws, export, live, stranger} <= set(keys(api.settings.s3_bucket, api))

    deleted = reconcile(api, delete=True)
    assert deleted.deleted >= 3
    assert deleted.delete_failed == 0
    remaining = set(keys(api.settings.s3_bucket, api))
    assert {lost, gone_ws, export}.isdisjoint(remaining)
    assert live in remaining  # referenced: kept
    assert stranger in remaining  # unknown: kept
    # The document still opens.
    docs = api.client.get(f"/api/v1/workspaces/{w}/documents", headers=h).json()
    assert docs["total"] == 1


def test_a_retained_migration_source_is_left_for_its_own_cleanup(
    iapi: Api, external_db: dict[str, Any], customer_bucket: dict[str, Any]
) -> None:
    """After a move to the customer's bucket, DocuMind's copy has no rows in the new
    data plane, but it is the retained source, not an orphan."""
    api = iapi
    w, h, elevated = owner_org(api)
    ready(api, w, h)
    retained = [k for k in keys(api.settings.s3_bucket, api) if w in k]
    _, mid = plan_migration(api, w, elevated, database=external_db, storage=customer_bucket)
    api.client.app.state.container.migration_dispatch = None  # type: ignore[attr-defined]
    api.client.post(url(w, f"/migrations/{mid}/approve"), headers=elevated)
    run_migration(api, mid)

    report = reconcile(api, delete=True)
    assert report.skipped["retained_migration_source"] >= len(retained)
    assert set(retained) <= set(keys(api.settings.s3_bucket, api))
