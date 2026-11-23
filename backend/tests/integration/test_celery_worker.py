"""Asynchronous processing through a real Celery worker process."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from documind.infrastructure.tasks.celery_dispatcher import CeleryJobDispatcher
from documind.worker.celery_factory import PROCESS_TASK, QUEUE, create_celery
from tests.integration.conftest import Api
from tests.integration.test_documents_api import upload
from tests.integration.test_processing_pipeline import contract_pdf, docs, ws

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _env(api: Api) -> dict[str, str]:
    s = api.settings
    env = {
        **os.environ,
        "ENVIRONMENT": "test",
        "LOG_LEVEL": "WARNING",
        "DATABASE_URL": s.database_url.get_secret_value(),
        "REDIS_URL": s.redis_url.get_secret_value(),
        "S3_BUCKET": s.s3_bucket,
        "EMBEDDING_PROVIDER": "local",
        "PROCESSING_RETRY_BASE_SECONDS": "1",
    }
    env.pop("CELERY_BROKER_URL", None)
    return env


@pytest.fixture
def worker_api(make_api: Callable[..., Api]) -> Iterator[Api]:
    settings_probe = make_api()
    dispatcher = CeleryJobDispatcher(
        create_celery(settings_probe.settings), task_name=PROCESS_TASK, queue=QUEUE
    )
    api = make_api(container_overrides={"job_dispatcher": dispatcher})
    worker = subprocess.Popen(  # noqa: S603 - fixed argv
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "documind.worker.celery_app",
            "worker",
            "--pool",
            "solo",
            "-Q",
            QUEUE,
            "--loglevel",
            "WARNING",
            "--without-heartbeat",
            "--without-gossip",
            "--without-mingle",
        ],
        cwd=BACKEND_DIR,
        env=_env(api),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        yield api
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=15)
        except subprocess.TimeoutExpired:
            worker.kill()


def test_upload_is_processed_by_a_celery_worker(worker_api: Api) -> None:
    api = worker_api
    ada = api.signup("ada@example.com")
    w = ws(api, ada)
    doc = upload(api, w, ada, contract_pdf("celery"), "msa.pdf").json()

    deadline = time.monotonic() + 90
    status = "pending"
    while time.monotonic() < deadline and status not in {"ready", "failed"}:
        time.sleep(0.5)
        status = api.client.get(docs(w, doc["id"]), headers=ada).json()["status"]
    assert status == "ready"
    job = api.client.get(docs(w, doc["id"], "processing"), headers=ada).json()["jobs"][0]
    assert job["status"] == "succeeded"
    assert job["attempts"] == 1
    assert job["stats"]["chunks"] > 0
