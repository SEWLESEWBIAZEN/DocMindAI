"""Azure Blob Storage against the Azurite emulator (MI-6).

Needs Azurite's blob service (``AZURITE_BLOB_URL``, default
http://127.0.0.1:10000/devstoreaccount1); skipped when it isn't running.
"""

# ruff: noqa: F811
from __future__ import annotations

import contextlib
import os
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient

from tests.integration.conftest import Api
from tests.integration.test_infrastructure import (  # noqa: F401 - fixtures
    activate_new,
    create,
    iapi,
    owner_org,
    ready,
    url,
)
from tests.integration.test_infrastructure_migration import (
    keys,
    plan_migration,
    run_migration,
)
from tests.integration.test_usage_and_quotas import POLICY

pytestmark = pytest.mark.integration

AZURITE_URL = os.environ.get("AZURITE_BLOB_URL", "http://127.0.0.1:10000/devstoreaccount1")
ACCOUNT = "devstoreaccount1"
# Azurite's published development key (not a secret).
ACCOUNT_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)


def _service() -> BlobServiceClient:
    return BlobServiceClient(
        AZURITE_URL, credential={"account_name": ACCOUNT, "account_key": ACCOUNT_KEY}
    )


@pytest.fixture
def azure_container() -> Iterator[dict[str, Any]]:
    service = _service()
    try:
        service.get_account_information()
    except Exception:
        pytest.skip("Azurite is not running")
    name = f"documind-{uuid.uuid4().hex[:12]}"
    container = service.get_container_client(name)
    container.create_container()
    yield {
        "provider": "azure_blob",
        "bucket": name,
        "endpoint_url": AZURITE_URL,
        "prefix": "acme/documind",
        "access_key_id": ACCOUNT,
        "secret_access_key": ACCOUNT_KEY,
    }
    container.delete_container()
    service.close()


def blob_names(container: str) -> list[str]:
    return [b.name for b in _service().get_container_client(container).list_blobs()]


def test_documents_in_an_azure_container(iapi: Api, azure_container: dict[str, Any]) -> None:
    api = iapi
    w, h, elevated = owner_org(api)
    providers = {p["key"]: p for p in api.client.get(url(w), headers=h).json()["storage_providers"]}
    assert providers["azure_blob"]["status"] == "available"
    version = activate_new(api, w, elevated, storage=azure_container)
    assert version["config"]["storage"]["provider"] == "azure_blob"
    assert ACCOUNT_KEY not in str(version)

    doc_id = ready(api, w, h)  # upload, extraction and indexing read and write Azure
    names = blob_names(azure_container["bucket"])
    assert names
    assert all(n.startswith(f"acme/documind/workspaces/{w}/") for n in names)
    hits = api.client.post(
        f"/api/v1/workspaces/{w}/search", json={"query": "terminate notice"}, headers=h
    ).json()
    assert hits["results"]

    signed = api.client.get(
        f"/api/v1/workspaces/{w}/documents/{doc_id}/download-url", headers=h
    ).json()["url"]
    assert azure_container["bucket"] in signed
    assert "sig=" in signed
    assert "sp=r" in signed  # read-only
    with urllib.request.urlopen(signed) as response:  # noqa: S310 - local emulator
        assert response.read() == POLICY
        assert response.headers["Content-Disposition"].startswith("attachment;")

    deleted = api.client.delete(f"/api/v1/workspaces/{w}/documents/{doc_id}", headers=elevated)
    assert deleted.status_code in (200, 204), deleted.text


def test_public_containers_and_wrong_keys_are_refused(
    iapi: Api, azure_container: dict[str, Any]
) -> None:
    api = iapi
    w, _, elevated = owner_org(api)
    wrong = {**azure_container, "secret_access_key": ACCOUNT_KEY[:-8] + "AAAAAAA="}
    vid = create(api, w, elevated, storage=wrong).json()["id"]
    result = api.client.post(url(w, f"/versions/{vid}/validate"), headers=elevated).json()
    assert result["status"] == "failed"
    assert result["validation"]["storage"]["code"] == "storage_rejected"

    public = f"public-{uuid.uuid4().hex[:12]}"
    service = _service()
    with contextlib.suppress(ResourceExistsError):
        service.create_container(public, public_access="blob")
    try:
        vid = create(api, w, elevated, storage={**azure_container, "bucket": public}).json()["id"]
        result = api.client.post(url(w, f"/versions/{vid}/validate"), headers=elevated).json()
        assert result["status"] == "failed"
        assert result["validation"]["storage"]["code"] == "bucket_public"
    finally:
        service.delete_container(public)


def test_migrating_from_documind_storage_to_azure(
    iapi: Api, azure_container: dict[str, Any]
) -> None:
    api = iapi
    w, h, elevated = owner_org(api)
    doc_id = ready(api, w, h)
    platform_keys = [k for k in keys(api.settings.s3_bucket, api) if w in k]
    vid, mid = plan_migration(api, w, elevated, storage=azure_container)
    api.client.app.state.container.migration_dispatch = None  # type: ignore[attr-defined]
    assert (
        api.client.post(url(w, f"/migrations/{mid}/approve"), headers=elevated).status_code == 202
    )
    report = run_migration(api, mid)
    assert report.objects == len(platform_keys)
    assert "documents" not in report.rows  # the database didn't move
    assert api.client.get(url(w), headers=h).json()["active_version_id"] == vid
    assert len(blob_names(azure_container["bucket"])) == len(platform_keys)
    signed = api.client.get(
        f"/api/v1/workspaces/{w}/documents/{doc_id}/download-url", headers=h
    ).json()["url"]
    with urllib.request.urlopen(signed) as response:  # noqa: S310 - local emulator
        assert response.read() == POLICY
