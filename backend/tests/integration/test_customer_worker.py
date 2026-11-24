# ruff: noqa: F811 - pytest fixtures imported from test_infrastructure
"""Customer-hosted processing end to end (MI-7).

The worker runs against the customer's own database and bucket, and talks to the
control plane only through the worker API (heartbeat, usage).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from documind.infrastructure.embeddings.local_provider import LocalHashingEmbeddingProvider
from documind.infrastructure.storage.s3_provider import S3ObjectStorage
from documind.worker.customer import (
    CustomerWorker,
    WorkerConfigError,
    WorkerRevokedError,
    parse_session,
)
from tests.integration.conftest import Api
from tests.integration.test_documents_api import upload
from tests.integration.test_infrastructure import (  # noqa: F401 - fixtures
    CUSTOMER_BUCKET,
    EXT_DB,
    EXT_PASSWORD,
    EXT_USER,
    _admin_url,
    _s3,
    create,
    customer_bucket,
    external_db,
    iapi,
    owner_org,
    plane_sql,
    url,
)
from tests.integration.test_processing_pipeline import sql
from tests.integration.test_usage_and_quotas import POLICY

pytestmark = pytest.mark.integration


class ApiControlPlane:
    """The real worker API, called through the test client (same requests as ControlPlane)."""

    def __init__(self, api: Api, token: str) -> None:
        self._api = api
        self._headers = {"Authorization": f"Bearer {token}"}

    async def heartbeat(self) -> Any:
        response = await asyncio.to_thread(
            self._api.client.post, "/api/v1/worker/v1/heartbeat", headers=self._headers
        )
        if response.status_code == 401:
            raise WorkerRevokedError("revoked")
        response.raise_for_status()
        return parse_session(response.json())

    async def report(
        self, job_id: uuid.UUID, *, model: str, input_tokens: int, outcome: str
    ) -> None:
        response = await asyncio.to_thread(
            self._api.client.post,
            "/api/v1/worker/v1/usage",
            json={
                "job_id": str(job_id),
                "embedding_model": model,
                "input_tokens": input_tokens,
                "outcome": outcome,
            },
            headers=self._headers,
        )
        if response.status_code == 401:
            raise WorkerRevokedError("revoked")
        response.raise_for_status()


class OtherModel(LocalHashingEmbeddingProvider):
    model = "some-other-model"


def run_worker(api: Api, token: str, embeddings: Any = None) -> int:
    async def go() -> int:
        engine = create_async_engine(
            _admin_url(api).set(database=EXT_DB, username=EXT_USER, password=EXT_PASSWORD),
            connect_args={"server_settings": {"search_path": "documind,public"}},
        )
        client = _s3(api.settings)
        try:
            worker = CustomerWorker(
                ApiControlPlane(api, token),
                engine,
                S3ObjectStorage(client, CUSTOMER_BUCKET, prefix="acme/documind"),
                embeddings or LocalHashingEmbeddingProvider(1536),
                name="customer-test",
            )
            return await worker.run_once()
        finally:
            await engine.dispose()
            client.close()

    return asyncio.run(go())


def test_customer_workers_process_in_the_customers_infrastructure(
    iapi: Api, external_db: dict[str, Any], customer_bucket: dict[str, Any]
) -> None:
    api = iapi
    w, h, elevated = owner_org(api)
    token = api.client.post(
        url(w, "/workers"), json={"name": "on-prem-1"}, headers=elevated
    ).json()["token"]

    body = {"database": external_db, "storage": customer_bucket, "processing": {"mode": "customer"}}
    vid = create(api, w, elevated, **body).json()["id"]
    first = api.client.post(url(w, f"/versions/{vid}/validate"), headers=elevated).json()
    assert first["validation"]["processing"]["code"] == "no_active_worker"

    assert run_worker(api, token) == 0  # checks in; nothing to do yet
    vid = create(api, w, elevated, **body).json()["id"]
    second = api.client.post(url(w, f"/versions/{vid}/validate"), headers=elevated).json()
    assert second["status"] == "validated", second["validation"]
    assert (
        api.client.post(url(w, f"/versions/{vid}/activate"), headers=elevated).json()["status"]
        == "active"
    )

    before = len(api.dispatcher.calls)
    doc = upload(api, w, h, POLICY, "policy.md").json()
    assert len(api.dispatcher.calls) == before  # never sent to DocuMind's workers
    assert plane_sql(api, EXT_DB, "SELECT status FROM processing_jobs")[0].status == "queued"

    # A worker embedding with another model would make search miss everything: refused.
    with pytest.raises(WorkerConfigError, match="same embedding model"):
        run_worker(api, token, OtherModel(1536))

    assert run_worker(api, token) == 1
    status = api.client.get(f"/api/v1/workspaces/{w}/documents/{doc['id']}", headers=h).json()[
        "status"
    ]
    assert status == "ready"
    hits = api.client.post(
        f"/api/v1/workspaces/{w}/search", json={"query": "terminate notice"}, headers=h
    )
    assert hits.json()["results"]
    usage = sql(
        api,
        "SELECT provider, billed_to, source_id FROM usage_records "
        "WHERE provider = 'customer_worker'",
    )
    assert [(u.billed_to, u.source_id) for u in usage] == [
        ("workspace_key", plane_sql(api, EXT_DB, "SELECT id FROM processing_jobs")[0].id.__str__())
    ]
    seen = sql(api, "SELECT last_seen_at FROM worker_registrations")[0].last_seen_at
    assert seen is not None

    # Revocation stops the worker at its next check-in.
    wid = sql(api, "SELECT id FROM worker_registrations")[0].id
    assert api.client.delete(url(w, f"/workers/{wid}"), headers=h).status_code == 204
    with pytest.raises(WorkerRevokedError):
        run_worker(api, token)
