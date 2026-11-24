"""Operations endpoints (Phase 10): Prometheus metrics."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from tests.integration.conftest import Api
from tests.integration.test_documents_api import upload
from tests.integration.test_processing_pipeline import ws
from tests.integration.test_usage_and_quotas import POLICY

pytestmark = pytest.mark.integration


def test_metrics_are_off_without_a_token(api: Api) -> None:
    assert api.client.get("/api/v1/metrics").status_code == 404


def test_metrics_need_the_token_and_report_operational_gauges(make_api: Any) -> None:
    api: Api = make_api(metrics_token=SecretStr("metrics-token-for-tests-0123456789"))
    h = api.signup("ada@example.com")
    upload(api, ws(api, h), h, POLICY, "policy.md")
    assert api.client.get("/api/v1/metrics").status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert api.client.get("/api/v1/metrics", headers=wrong).status_code == 401
    ok = api.client.get(
        "/api/v1/metrics", headers={"Authorization": "Bearer metrics-token-for-tests-0123456789"}
    )
    assert ok.status_code == 200
    body = ok.text
    assert ok.headers["content-type"].startswith("text/plain")
    for metric in (
        "documind_up 1",
        'documind_dependency_up{component="database"} 1',
        "documind_queue_depth",
        'documind_processing_jobs_24h{status="queued"} 1.0',
        "documind_oldest_queued_job_seconds",
        'documind_dataplanes{state="total"}',
        "documind_backup_last_success_timestamp 0.0",
    ):
        assert metric in body, metric
