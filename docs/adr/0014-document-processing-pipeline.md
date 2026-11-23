# ADR 0014: Document processing pipeline

- Status: Accepted
- Date: 2026-11-23

## Context

Phase 4 turns uploaded documents into searchable, citable chunks. The pipeline
is extraction → normalization → chunking → embedding → vector indexing. It
must be asynchronous, idempotent, retryable and observable, and it must never
let a committed document silently remain unprocessed. ADR 0011 adds two
requirements: OCR for scanned PDFs, and preserved page and heading structure.

## Decision

### Components

| Component | Implementation |
|---|---|
| DocumentExtractor | **PDF:** PyMuPDF 1.x. Text blocks in reading order; headings from the outline or inferred from font size and weight; metadata kept. Pages with no text layer but with images are OCR'd through PyMuPDF's built-in Tesseract integration (needs only Tesseract language data). **DOCX:** python-docx. Heading styles, tables and Word's recorded page breaks; a zip-bomb guard runs before parsing. **TXT/Markdown:** native, Markdown headings, code fences respected. |
| TextNormalizer | NFKC; control characters removed; hyphenated line breaks joined; whitespace collapsed; repeated running headers and footers removed. |
| DocumentChunker | Section-aware (a heading starts a chunk, and the heading path is kept as metadata). **Page-bounded by default**, so every citation names exactly one page. Paragraphs kept whole; oversized ones split at sentences, then words. Overlap uses whole trailing sentences. Token budget uses a conservative estimator (the tokenizer data isn't always downloadable). |
| EmbeddingService | Batched by count and token budget. Re-uses existing vectors for identical input (hash of model, dimensions and input text) within the workspace. |
| Embedding providers | `openai`: SDK with retries that honour `Retry-After`, plus error classification. `local`: deterministic feature hashing for development and tests, **rejected in production**. |
| VectorRepository | `document_chunks.embedding vector(1536)` with an HNSW cosine index, and `document_pages` for extracted text. |
| Orchestrator | Claims the job, runs the stages, writes the index, classifies failures. |

The embedding input is prefixed with the document title and heading path
(contextual embeddings). The stored chunk text stays the original passage, for
citations.

### Reliability

- **Transactional outbox.** The upload transaction inserts a `processing_jobs`
  row next to the document. Dispatch to Celery happens after commit and is
  best effort.
- **Sweeper** (Celery beat, every 30 s):
  - re-dispatches queued jobs not picked up within the grace period;
  - re-queues jobs whose lease expired (crashed worker), or marks them dead
    once their attempts are used up;
  - creates jobs for pending documents that have none.
- **Single owner.** `claim()` is one atomic `UPDATE … WHERE status='queued'
  AND due RETURNING`. Duplicate deliveries do nothing. A partial unique index
  allows at most one active job per document.
- **Idempotent.** A document's pages and chunks are replaced in one transaction.
- **Versions.** A new version supersedes the active job, and results are only
  written if the job is still running for the document's current version.
- **Failures.**
  - Permanent errors (damaged, encrypted, empty, unsafe or too large input;
    rejected content) → `failed` immediately.
  - Transient errors (rate limits, network, storage, unknown) → exponential
    backoff with jitter, up to `PROCESSING_MAX_ATTEMPTS`, then `dead` (the dead
    letter).
  - `POST …/reprocess` retries failed or dead documents manually.
- **Celery settings.** `acks_late` and `reject_on_worker_lost`; prefetch of 1;
  the visibility timeout is longer than the lease; time limits sit below the
  lease. Job state lives in PostgreSQL, so Celery results are disabled.
- **Async in workers.** Each worker process keeps one event loop and one
  container, so connection pools are reused across tasks.

### Observability

- `GET …/documents/{id}/processing` shows the job history: status, stage,
  attempts, error code and message, timings, and stats (pages, OCR pages,
  chunks, embeddings created and re-used, tokens, duration).
- `…/pages` and `…/chunks` expose the results.
- Structured logs per stage, keyed by job ID.
- CLI: `documind-manage jobs|sweep|process|reindex`.

## Alternatives

- *Celery result backend as the job store:* results expire, and querying them
  per document is awkward. A database table gives durability, an audit trail,
  and the outbox in one place.
- *Dispatch inside the upload transaction:* publishing to Redis can't take part
  in the database commit. The outbox plus sweeper is the standard fix.
- *A separate vector database:* rejected in ADR 0003. pgvector with HNSW keeps
  tenant filtering inside one SQL query (Phase 5).
- *PyMuPDF 2.x:* hard-depends on an ML layout stack (onnxruntime, numpy and
  others, hundreds of MB) that we don't use. 1.x has the same APIs.
- *pytesseract / ocrmypdf:* extra processes and dependencies. PyMuPDF's
  built-in OCR only needs language data.

## Consequences

- `EMBEDDING_DIMENSIONS` is fixed at 1536 (migration `0005`). OpenAI v3 models
  can all produce 1536-dimension vectors. Changing it needs a migration, then
  `documind-manage reindex --all`.
- Workers need Tesseract language data (installed in the images).
- The token estimate over-counts, so chunks are somewhat smaller than the budget.
- Figure and chart descriptions (ADR 0011) depend on a vision-capable LLM
  provider and move to Phase 5.
