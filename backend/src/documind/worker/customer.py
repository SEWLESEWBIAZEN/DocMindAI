"""Customer-hosted processing worker (MI-7, ADR 0024).

Runs inside the customer's network, next to their own database and storage::

    documind-worker            # reads its configuration from the environment

It connects **directly** to the organization's data plane (``DATAPLANE_URL``)
and bucket (``S3_*``), so those credentials never leave the customer's network.
It talks to DocuMind only to authenticate (``/worker/v1/heartbeat``), to learn
which embedding model the workspace searches with, and to report usage. A
revoked token stops the worker at the next heartbeat.

Environment:
    DOCUMIND_URL            https://documind.example.com
    DOCUMIND_WORKER_TOKEN   dmw_... (from Organization -> Infrastructure)
    DATAPLANE_URL           postgresql+asyncpg://user:pass@db.internal:5432/docs
    S3_ENDPOINT_URL, S3_REGION, S3_BUCKET, S3_PREFIX, S3_ACCESS_KEY_ID,
    S3_SECRET_ACCESS_KEY, S3_FORCE_PATH_STYLE
    EMBEDDING_PROVIDER      openai (with OPENAI_API_KEY, EMBEDDING_MODEL) or local
    OCR_ENABLED, OCR_LANGUAGES, WORKER_POLL_SECONDS (5), WORKER_NAME
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from dataclasses import dataclass
from typing import Any

import boto3
import httpx2 as httpx
from botocore.config import Config as BotoConfig
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from documind.application.identity.common import utcnow
from documind.application.ports import EmbeddingProvider
from documind.application.processing.embedding_service import EmbeddingService
from documind.application.processing.figures import FigureDescriber
from documind.application.processing.orchestrator import (
    ProcessingOrchestrator,
    SharedTargets,
    StaticProcessingAI,
)
from documind.core.config import Settings
from documind.core.logging import configure_logging
from documind.infrastructure.database.dataplane import DATAPLANE_SCHEMA
from documind.infrastructure.database.repositories.processing import (
    ProcessingJobRepository,
    SessionEmbeddingCache,
)
from documind.infrastructure.embeddings.local_provider import LocalHashingEmbeddingProvider
from documind.infrastructure.embeddings.openai_provider import OpenAIEmbeddingProvider
from documind.infrastructure.processing.extractors import (
    CompositeExtractor,
    ExtractionLimits,
    FigureOptions,
    OcrOptions,
)
from documind.infrastructure.storage.s3_provider import S3ObjectStorage

logger = logging.getLogger("documind.customer_worker")


class WorkerRevokedError(Exception):
    """The control plane no longer accepts this worker's token."""


class WorkerConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class WorkerSession:
    worker_id: uuid.UUID
    workspace_id: uuid.UUID
    scopes: tuple[str, ...]
    embedding_model: str
    embedding_dimensions: int


def parse_session(body: dict[str, Any]) -> WorkerSession:
    processing = body.get("processing") or {}
    return WorkerSession(
        worker_id=uuid.UUID(body["worker_id"]),
        workspace_id=uuid.UUID(body["workspace_id"]),
        scopes=tuple(body["scopes"]),
        embedding_model=processing.get("embedding_model", ""),
        embedding_dimensions=int(processing.get("embedding_dimensions", 1536)),
    )


class ControlPlane:
    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v1/worker/v1",
            headers={"Authorization": f"Bearer {token}", "User-Agent": "documind-worker"},
            timeout=30,
            transport=transport,
        )

    async def heartbeat(self) -> WorkerSession:
        response = await self._client.post("/heartbeat")
        if response.status_code == 401:
            raise WorkerRevokedError("The worker token was rejected (revoked or unknown).")
        response.raise_for_status()
        return parse_session(response.json())

    async def report(
        self, job_id: uuid.UUID, *, model: str, input_tokens: int, outcome: str
    ) -> None:
        response = await self._client.post(
            "/usage",
            json={
                "job_id": str(job_id),
                "embedding_model": model,
                "input_tokens": input_tokens,
                "outcome": outcome,
            },
        )
        if response.status_code == 401:
            raise WorkerRevokedError("The worker token was rejected (revoked or unknown).")
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise WorkerConfigError(f"Set {name}")
    return value


def embedding_provider_from_env() -> EmbeddingProvider:
    dims = int(os.environ.get("EMBEDDING_DIMENSIONS", "1536"))
    if os.environ.get("EMBEDDING_PROVIDER", "openai") == "local":
        return LocalHashingEmbeddingProvider(dims)
    return OpenAIEmbeddingProvider(
        api_key=_env("OPENAI_API_KEY"),
        model=os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
        dimensions=dims,
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
    )


def storage_from_env() -> tuple[S3ObjectStorage, Any]:
    client = boto3.session.Session().client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
        region_name=os.environ.get("S3_REGION", "us-east-1"),
        aws_access_key_id=_env("S3_ACCESS_KEY_ID"),
        aws_secret_access_key=_env("S3_SECRET_ACCESS_KEY"),
        config=BotoConfig(
            signature_version="s3v4",
            s3={
                "addressing_style": "path"
                if os.environ.get("S3_FORCE_PATH_STYLE") == "true"
                else "auto"
            },
        ),
    )
    return S3ObjectStorage(
        client, _env("S3_BUCKET"), prefix=os.environ.get("S3_PREFIX", "")
    ), client


def dataplane_engine_from_env() -> AsyncEngine:
    return create_async_engine(
        _env("DATAPLANE_URL"),
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "search_path": f"{DATAPLANE_SCHEMA},public",
                "application_name": "documind-customer-worker",
            }
        },
    )


class CustomerWorker:
    def __init__(
        self,
        control: Any,  # ControlPlane, or a test double with the same methods
        engine: AsyncEngine,
        storage: S3ObjectStorage,
        embeddings: EmbeddingProvider,
        *,
        settings: Settings | None = None,
        name: str | None = None,
    ) -> None:
        self._control = control
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        self._storage = storage
        self._embeddings = embeddings
        # Processing settings only (chunking, leases, retries): defaults, no secrets needed.
        self._settings = settings or Settings.model_construct()
        self._name = name or f"customer:{socket.gethostname()}:{os.getpid()}"

    def _orchestrator(self) -> ProcessingOrchestrator:
        s = self._settings
        extractor = CompositeExtractor(
            ExtractionLimits(max_pages=s.max_document_pages),
            OcrOptions(
                enabled=os.environ.get("OCR_ENABLED", "true") == "true",
                languages=os.environ.get("OCR_LANGUAGES", "eng"),
                dpi=s.ocr_dpi,
            ),
            FigureOptions(
                enabled=False, min_size_points=s.figure_min_size_points, max_per_document=0
            ),
        )
        embeddings = EmbeddingService(
            self._embeddings,
            SessionEmbeddingCache(self._sessions),
            batch_size=s.embedding_batch_size,
        )
        return ProcessingOrchestrator(
            self._sessions,
            s,
            self._storage,
            extractor,
            worker=self._name,
            ai=StaticProcessingAI(embeddings, FigureDescriber(None)),
            targets=SharedTargets(self._sessions, self._storage),
        )

    async def run_once(self) -> int:
        """Heartbeat, then process every due job of the workspace. Returns jobs handled."""
        session = await self._control.heartbeat()
        if "jobs:claim" not in session.scopes:
            raise WorkerConfigError("This worker token can't claim jobs.")
        if session.embedding_model and session.embedding_model != self._embeddings.model:
            raise WorkerConfigError(
                f"The workspace searches with '{session.embedding_model}' but this worker "
                f"embeds with '{self._embeddings.model}'. Configure the same embedding model."
            )
        async with self._sessions() as db:
            job_ids = await ProcessingJobRepository(db).claimable(session.workspace_id, utcnow())
        orchestrator = self._orchestrator()
        handled = 0
        for job_id in job_ids:
            outcome = await orchestrator.run(job_id, workspace_id=session.workspace_id)
            if outcome.status == "skipped":
                continue  # another worker (or DocuMind, in hybrid mode) claimed it
            handled += 1
            async with self._sessions() as db:
                job = await ProcessingJobRepository(db).get(job_id)
                tokens = int((job.stats or {}).get("embedding_input_tokens", 0)) if job else 0
            await self._control.report(
                job_id, model=self._embeddings.model, input_tokens=tokens, outcome=outcome.status
            )
            logger.info("job processed", extra={"job_id": str(job_id), "outcome": outcome.status})
        return handled

    async def run_forever(self, poll_seconds: float) -> None:
        while True:
            try:
                await self.run_once()
            except WorkerRevokedError:
                logger.error("worker token revoked: stopping")
                raise
            except (httpx.HTTPError, OSError) as exc:
                logger.warning(
                    "control plane or data plane unreachable; retrying",
                    extra={"error": type(exc).__name__},
                )
            await asyncio.sleep(poll_seconds)


def main() -> int:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"), os.environ.get("LOG_FORMAT", "json"))
    try:
        storage, client = storage_from_env()
        worker = CustomerWorker(
            ControlPlane(_env("DOCUMIND_URL"), _env("DOCUMIND_WORKER_TOKEN")),
            dataplane_engine_from_env(),
            storage,
            embedding_provider_from_env(),
            name=os.environ.get("WORKER_NAME"),
        )
    except WorkerConfigError as exc:
        logger.error(str(exc))
        return 2
    try:
        asyncio.run(worker.run_forever(float(os.environ.get("WORKER_POLL_SECONDS", "5"))))
    except WorkerRevokedError:
        return 3
    finally:
        client.close()
    return 0
