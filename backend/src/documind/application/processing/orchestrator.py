"""Runs one processing job end to end.

Upload → (validation, storage, job: Phase 3) → **extraction → normalization →
chunking → embedding → vector indexing → completion**.

Guarantees:

* **Single owner**: a job is claimed atomically; duplicate deliveries do nothing.
* **Idempotent**: a version's pages and chunks are replaced in one transaction.
* **Current version only**: if a newer version was uploaded meanwhile, the job
  is marked ``superseded`` and its results are discarded.
* **Classified failures**: permanent errors fail the document immediately;
  transient ones retry with exponential backoff until ``max_attempts``, after
  which the job is ``dead`` (dead letter) and can be retried manually.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.ai.metering import usage_source
from documind.application.identity.common import utcnow
from documind.application.ports import ObjectStorageProvider
from documind.application.processing.embedding_service import EmbeddedTexts, EmbeddingService
from documind.application.processing.figures import FigureDescriber
from documind.application.processing.jobs import at, retry_delay_seconds
from documind.core.config import Settings
from documind.domain.documents.files import DocumentFormat
from documind.domain.documents.status import DocumentStatus
from documind.domain.processing.chunking import (
    Chunk,
    ChunkingOptions,
    chunk_document,
    embedding_input,
)
from documind.domain.processing.errors import PermanentProcessingError, TransientProcessingError
from documind.domain.processing.model import BlockKind, ExtractedDocument
from documind.domain.processing.normalize import normalize_document
from documind.infrastructure.database.models import (
    DocumentChunk,
    DocumentPage,
    ProcessingJob,
)
from documind.infrastructure.database.repositories.documents import DocumentRepository
from documind.infrastructure.database.repositories.processing import (
    IndexRepository,
    ProcessingJobRepository,
)

logger = logging.getLogger(__name__)

Outcome = Literal["succeeded", "failed", "retry", "dead", "superseded", "cancelled", "skipped"]


class Extractor(Protocol):
    def extract(self, data: bytes, fmt: DocumentFormat) -> ExtractedDocument: ...


@dataclass(frozen=True, slots=True)
class JobOutcome:
    status: Outcome
    retry_in_seconds: int | None = None


class ProcessingAI(Protocol):
    """Per-workspace embedding and figure services (ADR 0019: workspace keys)."""

    async def for_workspace(
        self, workspace_id: uuid.UUID
    ) -> tuple[EmbeddingService, FigureDescriber]: ...


class StaticProcessingAI:
    """The same services for every workspace (tests and single-tenant tools)."""

    def __init__(self, embeddings: EmbeddingService, figures: FigureDescriber) -> None:
        self._embeddings = embeddings
        self._figures = figures

    async def for_workspace(
        self, workspace_id: uuid.UUID
    ) -> tuple[EmbeddingService, FigureDescriber]:
        return self._embeddings, self._figures


class ProcessingOrchestrator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        storage: ObjectStorageProvider,
        extractor: Extractor,
        embeddings: EmbeddingService | None = None,
        *,
        worker: str = "worker",
        figures: FigureDescriber | None = None,
        ai: ProcessingAI | None = None,
    ) -> None:
        if ai is None:
            if embeddings is None:
                raise ValueError("Pass either embeddings or a ProcessingAI")
            ai = StaticProcessingAI(embeddings, figures or FigureDescriber(None))
        self._ai = ai
        self._sessions = session_factory
        self._settings = settings
        self._storage = storage
        self._extractor = extractor
        self._worker = worker

    # ------------------------------------------------------------------ helpers

    async def _set_stage(self, job_id: uuid.UUID, stage: str) -> None:
        """Record progress and renew the lease (proof the worker is alive)."""
        async with self._sessions() as session:
            job = await ProcessingJobRepository(session).get(job_id, for_update=True)
            if job is not None and job.status == "running":
                job.stage = stage
                job.lease_expires_at = at(utcnow(), self._settings.processing_lease_seconds)
                await session.commit()
        logger.info("Processing stage", extra={"job_id": str(job_id), "stage": stage})

    @staticmethod
    def _finish(job: ProcessingJob, status: str, now: datetime) -> None:
        job.status = status
        job.finished_at = now
        job.lease_expires_at = None

    # ---------------------------------------------------------------------- run

    async def run(self, job_id: uuid.UUID) -> JobOutcome:
        started = time.perf_counter()
        async with self._sessions() as session:
            jobs = ProcessingJobRepository(session)
            job = await jobs.claim(
                job_id, utcnow(), self._settings.processing_lease_seconds, self._worker
            )
            if job is None:
                await session.commit()
                logger.info(
                    "Job not claimable (already handled or not due)", extra={"job_id": str(job_id)}
                )
                return JobOutcome("skipped")
            doc = await DocumentRepository(session).get(
                job.workspace_id, job.document_id, for_update=True
            )
            if doc is None:
                self._finish(job, "cancelled", utcnow())
                await session.commit()
                return JobOutcome("cancelled")
            if doc.current_version_id != job.document_version_id:
                self._finish(job, "superseded", utcnow())
                await session.commit()
                return JobOutcome("superseded")
            version = await DocumentRepository(session).get_version(
                job.workspace_id, job.document_id, job.document_version_id
            )
            if version is None:
                self._finish(job, "cancelled", utcnow())
                await session.commit()
                return JobOutcome("cancelled")
            doc.status = DocumentStatus.PROCESSING.value
            job.stage = "extracting"
            snapshot = (
                job.workspace_id,
                doc.id,
                version.id,
                version.storage_key,
                DocumentFormat(version.format),
                doc.title,
            )
            await session.commit()

        workspace_id, document_id, version_id, key, fmt, title = snapshot
        log = {"job_id": str(job_id), "document_id": str(document_id)}
        try:
            embeddings, figures = await self._ai.for_workspace(workspace_id)
            buffer = io.BytesIO()
            await self._storage.download_to(key, buffer)
            extracted = await asyncio.to_thread(self._extractor.extract, buffer.getvalue(), fmt)
            with usage_source("processing_job", job_id):
                extracted, _, _ = await figures.describe(extracted)
            document = normalize_document(extracted)
            if document.char_count == 0:
                raise PermanentProcessingError(
                    "No readable text was found in this document.", code="no_text"
                )

            await self._set_stage(job_id, "chunking")
            chunks = chunk_document(
                document,
                ChunkingOptions(
                    max_tokens=self._settings.chunk_max_tokens,
                    overlap_tokens=self._settings.chunk_overlap_tokens,
                    respect_pages=self._settings.chunk_respect_pages,
                ),
            )

            await self._set_stage(job_id, "embedding")
            with usage_source("processing_job", job_id):
                embedded = await embeddings.embed(
                    workspace_id, [embedding_input(title, c.heading_path, c.text) for c in chunks]
                )

            await self._set_stage(job_id, "indexing")
            return await self._complete(
                job_id,
                workspace_id,
                document_id,
                version_id,
                document,
                chunks,
                embedded,
                started,
                embeddings,
            )
        except PermanentProcessingError as exc:
            logger.warning("Processing failed permanently", extra={**log, "code": exc.code})
            return await self._fail(job_id, exc.code, exc.message, permanent=True)
        except TransientProcessingError as exc:
            logger.warning("Processing failed transiently", extra={**log, "code": exc.code})
            return await self._fail(job_id, exc.code, exc.message, permanent=False)
        except Exception:
            # Unknown errors are treated as transient; details go to the log only.
            logger.exception("Unexpected processing error", extra=log)
            return await self._fail(
                job_id,
                "internal_error",
                "An unexpected error occurred while processing.",
                permanent=False,
            )

    async def _complete(
        self,
        job_id: uuid.UUID,
        workspace_id: uuid.UUID,
        document_id: uuid.UUID,
        version_id: uuid.UUID,
        document: ExtractedDocument,
        chunks: list[Chunk],
        embedded: EmbeddedTexts,
        started: float,
        embeddings: EmbeddingService,
    ) -> JobOutcome:
        async with self._sessions() as session:
            jobs = ProcessingJobRepository(session)
            job = await jobs.get(job_id, for_update=True)
            doc = await DocumentRepository(session).get(workspace_id, document_id, for_update=True)
            now = utcnow()
            if job is None or job.status != "running":
                await session.commit()
                return JobOutcome("skipped")  # lease lost to another worker
            if doc is None:
                self._finish(job, "cancelled", now)
                await session.commit()
                return JobOutcome("cancelled")
            if doc.current_version_id != version_id:
                self._finish(job, "superseded", now)
                await session.commit()
                return JobOutcome("superseded")

            pages = [
                DocumentPage(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version_id=version_id,
                    position=p.position,
                    page_number=p.page_number,
                    text="\n\n".join(b.text for b in p.blocks),
                    headings=[b.text for b in p.blocks if b.kind is BlockKind.HEADING],
                    ocr=p.ocr,
                    char_count=sum(len(b.text) for b in p.blocks),
                )
                for p in document.pages
            ]
            rows = [
                DocumentChunk(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version_id=version_id,
                    chunk_index=c.index,
                    text=c.text,
                    token_count=c.token_count,
                    page_start=c.page_start,
                    page_end=c.page_end,
                    heading_path=list(c.heading_path),
                    content_sha256=h,
                    embedding=v,
                    embedding_model=embeddings.model,
                    embedding_dimensions=embeddings.dimensions,
                )
                for c, h, v in zip(chunks, embedded.content_hashes, embedded.vectors, strict=True)
            ]
            await IndexRepository(session).replace(document_id, pages, rows)

            doc.status = DocumentStatus.READY.value
            doc.processing_error = None
            doc.page_count = document.page_count
            doc.chunk_count = len(rows)
            doc.processed_at = now
            doc.extracted_title = document.title[:500] if document.title else None
            doc.extraction_warnings = list(document.warnings)
            doc.extracted_metadata = dict(document.metadata)
            job.stage = None
            job.error_code = None
            job.last_error = None
            job.stats = {
                "pages": len(pages),
                "ocr_pages": len(document.ocr_pages),
                "chunks": len(rows),
                "embeddings_reused": embedded.reused,
                "embeddings_created": embedded.embedded,
                "embedding_input_tokens": embedded.input_tokens,
                "embedding_model": embeddings.model,
                "duration_ms": round((time.perf_counter() - started) * 1000),
            }
            self._finish(job, "succeeded", now)
            await session.commit()
        logger.info("Processing succeeded", extra={"job_id": str(job_id), **job.stats})
        return JobOutcome("succeeded")

    async def _fail(
        self, job_id: uuid.UUID, code: str, message: str, *, permanent: bool
    ) -> JobOutcome:
        async with self._sessions() as session:
            job = await ProcessingJobRepository(session).get(job_id, for_update=True)
            if job is None or job.status != "running":
                await session.commit()
                return JobOutcome("skipped")
            doc = await DocumentRepository(session).get(
                job.workspace_id, job.document_id, for_update=True
            )
            now = utcnow()
            job.error_code = code
            job.last_error = message
            job.stage = None
            stale = doc is None or doc.current_version_id != job.document_version_id
            if permanent or job.attempts >= job.max_attempts:
                status = "failed" if permanent else "dead"
                self._finish(job, status, now)
                if doc is not None and not stale:
                    doc.status = DocumentStatus.FAILED.value
                    doc.processing_error = (
                        message
                        if permanent
                        else f"{message} (gave up after {job.attempts} attempts)"
                    )
                await session.commit()
                return JobOutcome(status)  # type: ignore[arg-type]

            delay = retry_delay_seconds(job.attempts, self._settings.processing_retry_base_seconds)
            job.status = "queued"
            job.lease_expires_at = None
            job.next_attempt_at = at(now, delay)
            # The worker re-schedules it with a countdown; the sweeper is the backstop.
            job.dispatched_at = job.next_attempt_at
            if doc is not None and not stale:
                doc.status = DocumentStatus.PENDING.value
                doc.processing_error = f"Retrying: {message}"
            await session.commit()
            return JobOutcome("retry", delay)
