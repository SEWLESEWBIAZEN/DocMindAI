"""Processing pipeline against real PostgreSQL/pgvector, Redis, MinIO and Tesseract."""

from __future__ import annotations

import asyncio
import io
import uuid
from collections.abc import Callable
from typing import Any

import docx
import pymupdf
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from documind.application.ports import EmbeddingBatch, EmbeddingProvider
from documind.application.processing.embedding_service import EmbeddingService
from documind.application.processing.orchestrator import JobOutcome, ProcessingOrchestrator
from documind.application.processing.sweeper import ProcessingSweeper, SweepResult
from documind.container import build_container
from documind.domain.documents.files import DocumentFormat
from documind.domain.processing.errors import TransientProcessingError
from documind.domain.processing.model import ExtractedDocument
from documind.infrastructure.database.repositories.processing import SessionEmbeddingCache
from documind.infrastructure.embeddings.local_provider import LocalHashingEmbeddingProvider
from documind.infrastructure.processing.extractors import find_tessdata
from tests.integration.conftest import Api, RecordingDispatcher
from tests.integration.test_documents_api import upload

pytestmark = pytest.mark.integration


# --- helpers ---


def sql(api: Api, statement: str, **params: Any) -> list[Any]:
    async def go() -> list[Any]:
        engine = create_async_engine(api.settings.database_url.get_secret_value())
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(statement), params)
                return list(result.all()) if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(go())


def run_job(
    api: Api,
    job_id: uuid.UUID | str,
    *,
    provider: EmbeddingProvider | None = None,
    extractor: Any = None,
) -> JobOutcome:
    async def go() -> JobOutcome:
        c = build_container(api.settings)
        try:
            embeddings = EmbeddingService(
                provider or c.embedding_provider, SessionEmbeddingCache(c.session_factory)
            )
            orchestrator = ProcessingOrchestrator(
                c.session_factory,
                api.settings,
                c.storage,
                extractor or c.extractor,
                embeddings,
                worker="test",
            )
            return await orchestrator.run(uuid.UUID(str(job_id)))
        finally:
            await c.aclose()

    return asyncio.run(go())


def sweep(api: Api, dispatcher: RecordingDispatcher) -> SweepResult:
    async def go() -> SweepResult:
        c = build_container(api.settings)
        try:
            return await ProcessingSweeper(c.session_factory, api.settings, dispatcher).sweep()
        finally:
            await c.aclose()

    return asyncio.run(go())


def ws(api: Api, headers: dict[str, str]) -> str:
    return str(api.client.get("/api/v1/me/workspaces", headers=headers).json()[0]["id"])


def docs(workspace: str, doc_id: str = "", tail: str = "") -> str:
    return (
        f"/api/v1/workspaces/{workspace}/documents"
        + (f"/{doc_id}" if doc_id else "")
        + (f"/{tail}" if tail else "")
    )


def latest_job(api: Api, workspace: str, doc_id: str, headers: dict[str, str]) -> dict[str, Any]:
    status = api.client.get(docs(workspace, doc_id, "processing"), headers=headers).json()
    job: dict[str, Any] = status["jobs"][0]
    return job


def contract_pdf(marker: str = "") -> bytes:
    pdf = pymupdf.open()
    content = [
        [
            ("Master Services Agreement", 20.0),
            ("Renewal", 14.0),
            ("This agreement renews automatically for successive one year terms.", 11.0),
            ("Notice of non-renewal must be given ninety days before expiry.", 11.0),
        ],
        [
            ("Fees", 14.0),
            ("Fees are payable within thirty days of invoice.", 11.0),
            (f"Reference {marker}.", 11.0),
        ],
    ]
    for lines in content:
        page = pdf.new_page()
        y = 72.0
        for line, size in lines:
            page.insert_text((72, y), line, fontsize=size, fontname="hebo" if size > 11 else "helv")
            y += size * 2.4
    out: bytes = pdf.tobytes()
    return out


class FailingProvider:
    model = "local-hashing-v1"
    dimensions = 1536

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        self.calls += 1
        raise TransientProcessingError(
            "Embedding service unavailable.", code="embedding_unavailable"
        )


# --- outbox and end-to-end processing ---


def test_upload_writes_job_in_same_transaction_and_dispatches(api: Api) -> None:
    ada = api.signup("ada@example.com")
    w = ws(api, ada)
    doc = upload(api, w, ada, contract_pdf("outbox"), "msa.pdf").json()
    job = latest_job(api, w, doc["id"], ada)
    assert job["status"] == "queued"
    assert job["attempts"] == 0
    assert api.dispatcher.dispatched == [uuid.UUID(job["id"])]


def test_pdf_is_extracted_chunked_embedded_and_indexed(api: Api) -> None:
    ada = api.signup("ada@example.com")
    w = ws(api, ada)
    doc = upload(api, w, ada, contract_pdf("e2e"), "msa.pdf").json()
    assert run_job(api, api.dispatcher.dispatched[0]).status == "succeeded"

    info = api.client.get(docs(w, doc["id"]), headers=ada).json()
    assert info["status"] == "ready"
    assert info["page_count"] == 2
    assert info["chunk_count"] >= 2
    assert info["processed_at"] is not None

    pages = api.client.get(docs(w, doc["id"], "pages"), headers=ada).json()
    assert pages["total"] == 2
    assert pages["items"][0]["page_number"] == 1
    assert "ninety days before expiry" in pages["items"][0]["text"]
    assert "Renewal" in pages["items"][0]["headings"]

    chunks = api.client.get(docs(w, doc["id"], "chunks"), headers=ada).json()["items"]
    renewal = next(c for c in chunks if "ninety days" in c["text"])
    assert (renewal["page_start"], renewal["page_end"]) == (1, 1)
    assert renewal["heading_path"] == ["Master Services Agreement", "Renewal"]
    fees = next(c for c in chunks if "thirty days" in c["text"])
    assert fees["page_start"] == 2
    assert fees["heading_path"][-1] == "Fees"

    rows = sql(
        api,
        "SELECT embedding_model, embedding_dimensions, vector_dims(embedding) FROM document_chunks",
    )
    assert {tuple(r) for r in rows} == {("local-hashing-v1", 1536, 1536)}
    job = latest_job(api, w, doc["id"], ada)
    assert job["status"] == "succeeded"
    assert job["stats"]["chunks"] == info["chunk_count"]
    assert job["stats"]["embeddings_created"] == info["chunk_count"]

    # pgvector cosine search finds the right passage (groundwork for Phase 5).
    query = asyncio.run(
        LocalHashingEmbeddingProvider(1536).embed(["notice of non-renewal ninety days"])
    ).vectors[0]
    best = sql(
        api,
        "SELECT text, page_start FROM document_chunks WHERE workspace_id = :w "
        "ORDER BY embedding <=> CAST(:q AS vector) LIMIT 1",
        w=w,
        q=str(query),
    )
    assert "ninety days" in best[0][0]
    assert best[0][1] == 1


def make_docx() -> bytes:
    d = docx.Document()
    d.add_heading("Vendor Policy", level=1)
    d.add_paragraph("Renewals above fifty thousand require Legal review.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize(
    ("content", "filename", "expected"),
    [
        (make_docx(), "policy.docx", "Legal review"),
        (b"# Runbook\n\nRestart the router after updating.\n", "runbook.md", "Restart the router"),
        (b"Plain notes about the quarterly budget.\n", "notes.txt", "quarterly budget"),
    ],
)
def test_other_formats(api: Api, content: bytes, filename: str, expected: str) -> None:
    ada = api.signup("ada@example.com")
    w = ws(api, ada)
    doc = upload(api, w, ada, content, filename).json()
    assert run_job(api, api.dispatcher.dispatched[0]).status == "succeeded"
    chunks = api.client.get(docs(w, doc["id"], "chunks"), headers=ada).json()["items"]
    assert any(expected in c["text"] for c in chunks)
    assert all(c["page_start"] is None for c in chunks)  # unpaginated formats


@pytest.mark.skipif(find_tessdata() is None, reason="Tesseract language data not installed")
def test_scanned_pdf_is_ocrd(api: Api) -> None:
    source = pymupdf.open()
    page = source.new_page()
    page.insert_text((72, 100), "Invoices are due within thirty days.", fontsize=16)
    image = page.get_pixmap(dpi=200)
    scanned = pymupdf.open()
    scanned.new_page(width=page.rect.width, height=page.rect.height).insert_image(
        page.rect, pixmap=image
    )
    ada = api.signup("ada@example.com")
    w = ws(api, ada)
    doc = upload(api, w, ada, scanned.tobytes(), "scan.pdf").json()
    assert run_job(api, api.dispatcher.dispatched[0]).status == "succeeded"
    first = api.client.get(docs(w, doc["id"], "pages"), headers=ada).json()["items"][0]
    assert first["ocr"] is True
    assert "thirty days" in first["text"]
    status = api.client.get(docs(w, doc["id"], "processing"), headers=ada).json()
    assert "ocr_applied:pages=1" in status["extraction_warnings"]


# --- idempotency, duplicates, versions ---


def test_duplicate_delivery_is_ignored(api: Api) -> None:
    ada = api.signup("ada@example.com")
    upload(api, ws(api, ada), ada, contract_pdf("dup"), "msa.pdf")
    job_id = api.dispatcher.dispatched[0]
    assert run_job(api, job_id).status == "succeeded"
    assert run_job(api, job_id).status == "skipped"


def test_reprocessing_is_idempotent_and_reuses_embeddings(api: Api) -> None:
    ada = api.signup("ada@example.com")
    w = ws(api, ada)
    doc = upload(api, w, ada, contract_pdf("idem"), "msa.pdf").json()
    run_job(api, api.dispatcher.dispatched[0])
    before = sql(api, "SELECT count(*) FROM document_chunks")[0][0]

    response = api.client.post(docs(w, doc["id"], "reprocess"), headers=ada)
    assert response.status_code == 202
    assert run_job(api, response.json()["job_id"]).status == "succeeded"
    assert (
        sql(api, "SELECT count(*) FROM document_chunks")[0][0] == before
    )  # replaced, not duplicated
    stats = latest_job(api, w, doc["id"], ada)["stats"]
    assert stats["embeddings_created"] == 0
    assert stats["embeddings_reused"] == before


def test_new_version_supersedes_queued_job(api: Api) -> None:
    ada = api.signup("ada@example.com")
    w = ws(api, ada)
    doc = upload(api, w, ada, contract_pdf("v1"), "msa.pdf").json()
    first = api.dispatcher.dispatched[0]
    api.client.post(
        docs(w, doc["id"], "versions"),
        files={"file": ("msa.md", b"# Version two\n\nUpdated terms apply.\n")},
        headers=ada,
    )
    second = api.dispatcher.dispatched[1]
    assert run_job(api, first).status == "skipped"  # superseded before it ran
    assert run_job(api, second).status == "succeeded"
    chunks = api.client.get(docs(w, doc["id"], "chunks"), headers=ada).json()["items"]
    assert any("Updated terms" in c["text"] for c in chunks)
    statuses = [
        j["status"]
        for j in api.client.get(docs(w, doc["id"], "processing"), headers=ada).json()["jobs"]
    ]
    assert statuses == ["succeeded", "superseded"]


def test_new_version_uploaded_mid_run_discards_stale_results(api: Api) -> None:
    ada = api.signup("ada@example.com")
    w = ws(api, ada)
    doc = upload(api, w, ada, contract_pdf("race"), "msa.pdf").json()
    real = build_container(api.settings).extractor

    class UploadsDuringExtraction:
        def extract(self, data: bytes, fmt: DocumentFormat) -> ExtractedDocument:
            api.client.post(
                docs(w, doc["id"], "versions"),
                files={"file": ("new.md", b"# New\n\nFresh content.\n")},
                headers=ada,
            )
            return real.extract(data, fmt)

    assert (
        run_job(api, api.dispatcher.dispatched[0], extractor=UploadsDuringExtraction()).status
        == "skipped"
    )
    assert (
        sql(api, "SELECT count(*) FROM document_chunks")[0][0] == 0
    )  # stale v1 results never written
    assert run_job(api, api.dispatcher.dispatched[1]).status == "succeeded"


# --- failures, retries, dead letters ---


def test_permanent_failure_is_not_retried(api: Api) -> None:
    ada = api.signup("ada@example.com")
    w = ws(api, ada)
    doc = upload(api, w, ada, b"%PDF-1.7\nnot actually a pdf body\n", "broken.pdf").json()
    assert run_job(api, api.dispatcher.dispatched[0]).status == "failed"
    status = api.client.get(docs(w, doc["id"], "processing"), headers=ada).json()
    assert status["status"] == "failed"
    assert status["processing_error"]
    job = status["jobs"][0]
    assert job["status"] == "failed"
    assert job["attempts"] == 1
    assert job["error_code"] in {"corrupt_file", "empty_document"}


def test_transient_failures_retry_then_dead_letter_then_manual_retry(
    make_api: Callable[..., Api],
) -> None:
    api = make_api(processing_max_attempts=2)
    ada = api.signup("ada@example.com")
    w = ws(api, ada)
    doc = upload(api, w, ada, contract_pdf("retry"), "msa.pdf").json()
    job_id = api.dispatcher.dispatched[0]
    failing = FailingProvider()

    first = run_job(api, job_id, provider=failing)
    assert first.status == "retry"
    assert first.retry_in_seconds is not None
    status = api.client.get(docs(w, doc["id"], "processing"), headers=ada).json()
    assert status["status"] == "pending"
    assert status["processing_error"].startswith("Retrying:")
    assert status["jobs"][0]["status"] == "queued"
    assert run_job(api, job_id, provider=failing).status == "skipped"  # backoff not elapsed

    sql(api, "UPDATE processing_jobs SET next_attempt_at = now() - interval '1 second'")
    assert run_job(api, job_id, provider=failing).status == "dead"
    status = api.client.get(docs(w, doc["id"], "processing"), headers=ada).json()
    assert status["status"] == "failed"
    assert "gave up after 2 attempts" in status["processing_error"]
    assert status["jobs"][0]["status"] == "dead"

    retry = api.client.post(docs(w, doc["id"], "reprocess"), headers=ada)
    assert retry.status_code == 202
    assert run_job(api, retry.json()["job_id"]).status == "succeeded"
    assert api.client.get(docs(w, doc["id"]), headers=ada).json()["status"] == "ready"


def test_reprocess_rules(api: Api) -> None:
    owner = api.signup("owner@example.com")
    org = str(api.create_org(owner)["id"])
    viewer = api.add_member(org, owner, "vic@example.com", "viewer")
    doc = upload(api, org, owner, contract_pdf("rules"), "msa.pdf").json()
    busy = api.client.post(docs(org, doc["id"], "reprocess"), headers=owner)
    assert busy.status_code == 409
    assert busy.json()["code"] == "processing_in_progress"
    assert api.client.post(docs(org, doc["id"], "reprocess"), headers=viewer).status_code == 403
    assert api.client.get(docs(org, doc["id"], "pages"), headers=viewer).status_code == 200


def test_processing_data_is_isolated_between_workspaces(api: Api) -> None:
    ada = api.signup("ada@example.com")
    w = ws(api, ada)
    doc = upload(api, w, ada, contract_pdf("iso"), "msa.pdf").json()
    run_job(api, api.dispatcher.dispatched[0])
    eve = api.signup("eve@example.com")
    for tail in ("processing", "pages", "chunks"):
        assert api.client.get(docs(w, doc["id"], tail), headers=eve).status_code == 404
        assert api.client.get(docs(ws(api, eve), doc["id"], tail), headers=eve).status_code == 404
    assert (
        api.client.post(docs(ws(api, eve), doc["id"], "reprocess"), headers=eve).status_code == 404
    )


def test_deleting_a_document_removes_its_index(api: Api) -> None:
    ada = api.signup("ada@example.com")
    w = ws(api, ada)
    doc = upload(api, w, ada, contract_pdf("del"), "msa.pdf").json()
    run_job(api, api.dispatcher.dispatched[0])
    assert api.client.delete(docs(w, doc["id"]), headers=api.elevated(ada)).status_code == 204
    for table in ("document_chunks", "document_pages", "processing_jobs"):
        assert sql(api, f"SELECT count(*) FROM {table}")[0][0] == 0  # noqa: S608 - fixed table names


# --- recovery: lost dispatches, crashed workers, missing jobs ---


def test_sweeper_redispatches_when_publishing_failed(make_api: Callable[..., Api]) -> None:
    broken = RecordingDispatcher(fail=True)
    api = make_api(container_overrides={"job_dispatcher": broken})
    ada = api.signup("ada@example.com")
    upload(api, ws(api, ada), ada, contract_pdf("lost"), "msa.pdf")
    assert broken.dispatched == []
    recovery = RecordingDispatcher()
    assert sweep(api, recovery).dispatched == 0  # within the grace period
    sql(api, "UPDATE processing_jobs SET dispatched_at = now() - interval '10 minutes'")
    assert sweep(api, recovery).dispatched == 1
    assert run_job(api, recovery.dispatched[0]).status == "succeeded"


def test_sweeper_recovers_jobs_from_crashed_workers(api: Api) -> None:
    ada = api.signup("ada@example.com")
    w = ws(api, ada)
    doc = upload(api, w, ada, contract_pdf("crash"), "msa.pdf").json()
    # A worker claimed the job and died: running, lease expired.
    sql(
        api,
        "UPDATE processing_jobs SET status = 'running', attempts = 1,"
        " lease_expires_at = now() - interval '1 minute'",
    )
    recovery = RecordingDispatcher()
    result = sweep(api, recovery)
    assert (result.requeued, result.dispatched) == (1, 1)
    assert run_job(api, recovery.dispatched[0]).status == "succeeded"
    assert api.client.get(docs(w, doc["id"]), headers=ada).json()["status"] == "ready"


def test_sweeper_creates_missing_jobs(api: Api) -> None:
    ada = api.signup("ada@example.com")
    upload(api, ws(api, ada), ada, contract_pdf("missing"), "msa.pdf")
    sql(api, "DELETE FROM processing_jobs")  # e.g. documents from before the pipeline
    recovery = RecordingDispatcher()
    result = sweep(api, recovery)
    assert (result.created, result.dispatched) == (1, 1)
    assert run_job(api, recovery.dispatched[0]).status == "succeeded"
