# Implementation status

Running checklist across all phases. A box is ticked only when the feature
works and has been verified by the listed evidence.

Legend: `[x]` done and verified · `[~]` partially done (see notes) · `[ ]` not started

## Phase 1: Architecture and project foundation ✅

| # | Task | Status | Evidence |
|---|------|--------|----------|
| 1 | Monorepo structure | [x] | `backend/`, `frontend/`, `infra/`, `docs/`, root Compose |
| 2 | React frontend (Vite + TS) | [x] | `npm run lint/typecheck/test/build` pass; served by Compose; browser-verified |
| 3 | FastAPI backend | [x] | App factory, versioned router, lifespan-managed container |
| 4 | `pyproject.toml` + lockfile | [x] | uv + `uv.lock`; `uv sync --locked` passes |
| 5 | PostgreSQL + pgvector | [x] | `pgvector/pgvector:0.8.7-pg16`; extension in app and test DBs; cosine-distance query test |
| 6 | Redis | [x] | `redis:7.4-alpine` with password + AOF; round-trip integration test |
| 7 | MinIO | [x] | Private bucket bootstrap; anonymous access returns 403. See ADR 0006 for the image caveat |
| 8 | Docker Compose | [x] | All 5 services healthy; ports bound to 127.0.0.1 |
| 9 | Environment configuration | [x] | Typed settings, required secrets, production safeguards |
| 10 | Logging | [x] | JSON/console formatters, request IDs, access logging, safe 500s |
| 11 | Dev and test environments | [x] | Makefile, unit vs integration markers, separate test DB, CI workflow |

Acceptance criteria (verified 2026-11-23):

- [x] All infrastructure containers start successfully (postgres, redis, minio healthy).
- [x] Frontend starts successfully (Compose `frontend` serves the app; Playwright check of healthy and degraded states).
- [x] Backend starts successfully (Compose `backend` healthy; runs migrations and bucket bootstrap on start).
- [x] Database connection verified (readiness check plus integration tests).
- [x] Health endpoint reports application and dependency health (200 when healthy; 503 when Redis was stopped; back to 200 after restart without an API restart).
- [x] No secrets committed (`.env` ignored; only development placeholders in `.env.example`, which production rejects).

### Phase 1 follow-ups / known issues

- **MinIO image (ADR 0006).** The upstream `minio/minio` Docker Hub image no
  longer exists. Development uses the frozen `bitnamilegacy/minio:2025.7.23`.
  The production storage decision is required before Phase 10.
- **Development uses MinIO root credentials for the backend.** A least-privilege
  service account for the bucket is a Phase 10 item.
- **Starlette TestClient deprecation.** Starlette now warns that using `httpx`
  with its TestClient is deprecated. The warning is filtered in
  `pyproject.toml`. Migrate the test client after reviewing the recommended
  replacement package.
- **CI workflow** (`.github/workflows/ci.yml`) is written but was not run in
  the build environment. Its steps mirror commands that were run locally.

## Phase 2: Database and identity architecture ✅

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Entities: User, Workspace, Organization, WorkspaceMember, Role, Permission, RefreshToken, AuditLog | [x] | Migration `0002`, plus AccountToken (verification/reset) and Invitation. `alembic check` reports no drift; upgrade → downgrade → upgrade tested |
| Registration (creates personal workspace) | [x] | `test_registration_creates_user_and_personal_workspace` |
| Login / logout / logout everywhere | [x] | `test_login_*`, `test_logout_*` |
| Argon2id hashing (with rehash on parameter change) | [x] | `test_security_adapters.py`, stored-hash assertion |
| Access-token issuance (JWT, strict claims) | [x] | Forged, expired, wrong-audience, `alg:none` and tampered tokens rejected |
| Refresh-token rotation and revocation, with reuse detection | [x] | `test_refresh_rotates_token`, `test_reused_refresh_token_revokes_the_session`, grace-window test; verified in Chromium |
| Password reset | [x] | `test_password_reset_flow`, single-use and latest-link-only tests |
| Email verification | [x] | Verification required before login; single-use and expiry tests; real SMTP via Mailpit end to end |
| Profile management, password change | [x] | `test_profile_update`, `test_change_password` |
| Create organizations, join via invitations | [x] | `test_invitation_flow`, email binding, decline/revoke/re-invite |
| Switch workspaces | [x] | `PUT /me/active-workspace`; non-member → 404 |
| Manage members, assign roles, leave with ownership rules | [x] | Hierarchy, last-owner and remove/leave tests |
| Owner / Admin / Member / Viewer via explicit permission policies | [x] | Permission catalog plus DB seed equality test; role matrix over HTTP |
| Authorization enforced in backend services | [x] | `ctx.require(...)` in services; policy unit tests |
| IDOR and cross-tenant protection | [x] | Cross-workspace 404 matrix; foreign member and invitation IDs not found |
| Secure refresh storage and cookies | [x] | Hash-only storage; `HttpOnly; Secure; SameSite=Strict; Path=/api/v1/auth` verified in browser |
| Account enumeration protection | [x] | Uniform register, resend and forgot responses; identical login errors; dummy hash |
| Rate limiting on auth endpoints | [x] | Per-email and per-IP 429 tests with `Retry-After` |
| Audit log | [x] | Security events recorded transactionally; paginated `GET /audit-logs` |

Acceptance criteria (verified 2026-11-23):

- [x] Registration creates a user and personal workspace.
- [x] Users can create organizations.
- [x] Users can belong to multiple workspaces.
- [x] Workspace switching works.
- [x] Unauthorized cross-workspace access is rejected (404 without revealing existence).
- [x] Authentication and authorization tests pass: 104 unit + 51 integration (backend); live end-to-end flows via Mailpit and Chromium.

### Phase 2 follow-ups / known issues

- Logging out one device leaves that access token valid for up to 15 minutes
  ("sign out everywhere" is immediate). Documented in ADR 0008.
- Rate limiting fails open if Redis is down (logged).
- Email change and account deletion are not implemented yet.
- Organization deletion is not implemented. Ownership transfer works by
  promoting another owner and then leaving.
- Emails are sent via FastAPI background tasks; move them to Celery with
  retries in Phase 4.
- Frontend sign-in and workspace screens arrive in Phase 7. The `/login` and
  `/register` pages still show "coming soon". The SPA must send
  `X-CSRF-Protection: 1` and make concurrent refresh calls single-flight.
- PostgreSQL row-level security as defence in depth: evaluate in Phase 3/9 (ADR 0009).

## Phase 3: Document management and storage ✅

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Upload documents | [x] | `test_upload_*`; browser FormData upload against the live stack |
| List / retrieve metadata | [x] | `GET /documents`, `GET /documents/{id}` |
| Download via signed URLs | [x] | `test_signed_download_url`; browser download from MinIO with the original filename |
| Delete (rows and stored objects) | [x] | `test_rename_and_delete`, `test_versions` (all versions' objects removed) |
| Rename | [x] | `PATCH /documents/{id}` |
| Collections (CRUD, add/remove, filter) | [x] | `test_collections` |
| Search by metadata | [x] | `q` over title and filename (LIKE wildcards escaped), status, format, collection, uploader, sort, pagination |
| Track processing status | [x] | `pending` on upload and on each new version; `processing_error`, `page_count` ready for Phase 4 |
| PDF, DOCX, TXT, Markdown | [x] | Signature and structure validation per format |
| `ObjectStorageProvider` (upload, download, delete, signed URL, metadata) + S3/boto3 adapter | [x] | `application/ports.py`, `infrastructure/storage/s3_provider.py` |
| Storage config independent of business logic | [x] | Port plus container wiring; public endpoint for signing |
| MIME and signature validation | [x] | Spoofed exe, mismatched extension, macro DOCX, non-UTF-8 text all rejected (415) |
| File-size limits | [x] | Service (413) and middleware cut-off before the body is read (413) |
| Internal object keys; path traversal prevented | [x] | `../../etc/evil.pdf` stored as `evil.pdf` under a generated key |
| Private buckets; short-lived signed URLs | [x] | Unsigned, tampered and expired URLs return 403 |
| No storage credentials in the frontend | [x] | Presign-only client; the browser receives only signed URLs |
| Malware scanning where feasible | [x] | `MalwareScanner` port; ClamAV adapter (protocol-tested); blocked and fail-closed tests |
| Uploaded content treated as untrusted | [x] | Attachment-only downloads with a forced content type; bidi and control characters stripped |
| Document, DocumentCollection, DocumentCollectionItem, DocumentVersion | [x] | Migration `0003`; composite FKs reject cross-tenant links at the DB level (tested) |

Acceptance criteria (verified 2026-11-23):

- [x] Users can upload supported files.
- [x] Documents are stored in MinIO.
- [x] Documents are isolated by workspace (API 404s plus database composite FKs).
- [x] Signed download URLs work (and expire).
- [x] Document metadata is persisted.
- [x] File validation works.
- [x] Unauthorized downloads are blocked.

Tests: 151 unit and 78 integration (backend, all passing); live browser upload, search, download and delete against the Compose stack.

### Phase 3 follow-ups / known issues

- ClamAV is opt-in (`MALWARE_SCANNER=clamav`) because clamd needs about 1–1.5 GB of
  RAM; the adapter is tested against a protocol-level fake, not a live clamd.
- Orphaned objects (if an object delete fails after commit) are logged; add a sweeper.
- Duplicate detection is exact-bytes, per workspace.
- ~~In organizations, Members can delete any document~~ → resolved by the change request below.
- Production Nginx must set `client_max_body_size` to match `MAX_UPLOAD_BYTES` (Phase 10).
- A filename containing `"` is received as `%22` (browsers encode it in multipart per the
  HTML spec); it is displayed as sent.

## Change request (2026-11-23): archiving and 2FA-protected deletion ✅

Product decision: *Members can archive, not delete, documents they uploaded.
Admins can delete any document, with 2FA.* See ADR 0013.

- [x] Permissions `documents.archive_own` (Member+) and `documents.archive_any` (Admin+);
      Members lose `documents.delete` (migration `0004`, frozen data, up/down tested)
- [x] Archive and restore endpoints; archived documents hidden by default, read-only, downloadable
- [x] TOTP 2FA: setup (password), confirm (code), 10 recovery codes, disable (password + factor),
      regenerate; RFC 6238 test vectors; replay protection; AES-GCM-encrypted secrets; HMAC-hashed
      recovery codes; per-user rate limits; audit events; security emails
- [x] Step-up: `POST /auth/mfa/verify` issues an elevated access token (`mfa` claim, 5 minutes);
      refreshed tokens are never elevated
- [x] `DELETE` document requires Admin/Owner **and** recent 2FA; same rule for Owners and personal workspaces
- [x] Tests: 186 unit + 97 integration (backend); live end-to-end against the Compose stack
      (archive, 403 paths, enrollment, real SMTP security email, step-up delete, object removed)

Follow-ups:

- [ ] Require 2FA at sign-in for users who enabled it (recommended), and optionally enforce
      enrollment for Admins/Owners per organization
- [ ] Support-assisted 2FA reset for users who lose both authenticator and recovery codes
- [ ] Passkeys (WebAuthn) as an additional factor; email OTP as an accessible fallback (ADR 0011)
- [ ] Frontend 2FA screens (QR code, manual secret, recovery codes) in Phase 7

## Phase 4: Document processing engine ✅

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Celery + Redis; asynchronous processing | [x] | Real Celery worker test; live stack (`worker` and `beat` services) |
| Reliable dispatch (no silently unprocessed documents) | [x] | Outbox job in the upload transaction; sweeper re-dispatch test; live run with the worker stopped and restarted |
| DocumentExtractor: PyMuPDF, python-docx, TXT/Markdown | [x] | `test_extractors.py`; pipeline tests per format |
| Page numbers, title, section headings, metadata, warnings preserved | [x] | `document_pages`, chunk `page_start`/`page_end`/`heading_path`, `extraction_warnings`, `extracted_metadata` |
| OCR for scanned PDFs (required by ADR 0011) | [x] | Tesseract via PyMuPDF; unit, integration and live (inside the worker container) |
| TextNormalizer | [x] | Hyphenation, whitespace, control characters, repeated headers/footers |
| DocumentChunker: max tokens, overlap, page boundaries, section-aware, metadata | [x] | `test_chunking.py` (20 tests) |
| EmbeddingService: batching, rate limits, retries, model/dimensions tracked, no duplicate work, re-indexing | [x] | Mocked-HTTP tests of the OpenAI adapter; content-hash re-use (reprocess creates 0 new embeddings); `documind-manage reindex` |
| VectorRepository (pgvector, HNSW cosine) | [x] | Migration `0005`; cosine-search test returns the right chunk |
| Orchestrator: idempotent, retry policy, failure tracking, status, observability | [x] | Duplicate delivery, superseded (including mid-run), permanent vs transient, backoff then dead then manual retry |
| Dead-letter / failed-job handling | [x] | `dead` status; `POST …/reprocess`; `documind-manage jobs --status dead` |
| Recovery from worker restarts | [x] | Expired-lease sweep test; `acks_late` + `reject_on_worker_lost`; live stop/start |

Acceptance criteria (verified 2026-11-23):

- [x] Uploaded documents are processed asynchronously.
- [x] Extracted text is available (`GET …/pages`).
- [x] Chunks are stored (`GET …/chunks`).
- [x] Embeddings are stored in pgvector (1536-d, HNSW). **Note:** verified with the local development provider; the OpenAI adapter is verified against mocked HTTP only, because `api.openai.com` is not reachable from this build environment and no key is configured.
- [x] Page metadata is preserved.
- [x] Failed jobs can be retried.
- [x] Duplicate processing is prevented.

Tests: 236 unit and 116 integration tests (backend), including a real Celery worker; live end-to-end on the Compose stack.

### Phase 4 follow-ups / known issues

- Live verification against the real OpenAI API is pending (network policy, no key).
- Token counts are estimates (conservative); a tokenizer can replace the estimator when its data is available.
- The image's `apt-get install tesseract-ocr` step couldn't run here (`deb.debian.org` is blocked by this
  environment's network policy). OCR was verified with the same language data mounted from the host; CI builds the images.
- Figure and chart descriptions (ADR 0011) moved to Phase 5, then deferred to Phase 6b (see Phase 5).
- The development worker does not auto-reload; restart it after backend code changes (`docker compose restart worker`).

## Phase 5: AI search and RAG engine ✅

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Query embedding through the provider abstraction | [x] | `RetrievalService`; embedding failures → 503 `embedding_unavailable` |
| Vector search with tenant filtering **inside the query** | [x] | `repositories/retrieval.py`: workspace on chunk and document, `ready`, not archived, current version, embedding model; isolation tests for other workspaces, archived, unprocessed, superseded and other-model chunks |
| Hybrid search (vector + keyword) | [x] | Migration `0006` (generated `tsvector` + GIN); RRF fusion; exact-identifier test; `-term` exclusion test |
| Document / collection scoping | [x] | Filters validated first (foreign or unknown id → 404); warnings for selected archived or unready documents |
| Context builder | [x] | Numbered `<source>` blocks, token budget, near-duplicate skip, escaping and tag neutralization (`test_rag_domain.py`) |
| Prompt manager (versioned) | [x] | `rag-answer-v1`, returned with every answer; system prompt sent unchanged (integration test) |
| LLM provider abstraction | [x] | `LLMProvider` port; OpenAI Chat Completions adapter (mocked HTTP: generate, SSE stream with usage, error classes); offline extractive provider |
| Citation resolver | [x] | Markers verified against the sources sent; invalid markers removed; excerpt, page and section labels |
| Speakable citation labels (ADR 0011) | [x] | `spoken_label`: "MSA, pages 14 to 15" |
| Speech-to-text / text-to-speech ports (ADR 0011) | [x] | `SpeechToTextProvider`, `TextToSpeechProvider` in `application/ports.py` (adapters in Phase 6b) |
| Insufficient-context handling | [x] | No passages → no model call (spy test); sentinel → `insufficient_context`, no citations, sentinel never shown or streamed |
| Prompt-injection resistance | [x] | Document trying to close `<source>` and inject `<system>` stays inside its block (integration and live); same for the question |
| Streaming-ready orchestrator | [x] | `RAGOrchestrator.stream()`: sentinel split across deltas is never shown; final verified answer event |
| API | [x] | `POST /workspaces/{id}/search` (documents.read), `POST /workspaces/{id}/ask` (conversations.use); per-user AI rate limit (429 test); request validation |

Acceptance criteria (verified 2026-11-23):

- [x] Semantic search returns relevant chunks (including OCR text from a scanned page, live).
- [x] Answers are grounded in retrieved chunks; every citation points to a real current chunk and page.
- [x] Answers span multiple documents with separate citations.
- [x] Insufficient evidence is reported instead of guessed.
- [x] Retrieval never crosses workspaces or includes archived, unprocessed or superseded content.
- [x] Instructions inside documents are treated as data.
- [x] **Not yet verified against the real OpenAI API.** `api.openai.com` is blocked by this environment's
      network policy and no key is configured. The adapters are tested against mocked HTTP; the pipeline
      ran end to end with the local providers.

Tests: 273 unit and 138 integration tests (backend); live end-to-end on the Compose stack
(real Celery worker with OCR → search → ask → isolation → archive exclusion, 15/15 checks).

### Phase 5 follow-ups / known issues

- Answer quality with the real model is unmeasured. An evaluation set (questions, expected pages, "no
  answer" cases) should run against OpenAI once a key is available; tune `RETRIEVAL_MIN_SCORE` and
  `RETRIEVAL_TOP_K` with it.
- The local providers are lexical only (no stemming or semantics). They demonstrate the plumbing, not
  quality; the "LLM" quotes matching sentences.
- Keyword search uses the `english` configuration; per-workspace language is a follow-up.
- Streamed text can briefly contain a citation marker that verification then removes; Phase 6 clients
  render the final answer from the completion event.
- Figure and chart descriptions (ADR 0011) are deferred to Phase 6b: they need a vision model call per
  image, which belongs with the accessibility work.
- Answers are not persisted yet (conversations, messages and citations come in Phase 6); usage is
  returned per response but not recorded (Phase 8).
- No search or answer UI yet (Phases 6 and 7).

## Phase 6: Conversation and chat system ✅

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Conversation, ConversationDocument, Message, MessageCitation (+ ConversationCollection) | [x] | Migration `0007` (round-trip and `alembic check` clean); composite tenant FKs |
| Create, rename, delete, list/search, retrieve history | [x] | `test_conversation_lifecycle`; paging by sequence (`before`, `has_more`) |
| Continue a conversation (memory) | [x] | Earlier turns in the prompt, markers stripped; follow-up condensation with fallback (`test_follow_up_…`, `test_conversation_rag.py`) |
| Select documents / collections | [x] | Union scope; selection never widens to the workspace when its sources are deleted (`selection_empty`) |
| Stream responses (SSE) | [x] | Event sequence via TestClient; real uvicorn server; live through the Vite proxy (first delta ≈ 0.07 s) |
| Citations recorded and displayed | [x] | Snapshot citations; `document_available` after archive; readable after document deletion |
| Regenerate where appropriate | [x] | Latest answer only; old answer kept as superseded; 409 for older answers and questions |
| Cancellation | [x] | `POST …/cancel` → Redis flag → generation stops, partial saved as `cancelled` (real-server test) |
| Client disconnects | [x] | Generation stops; `cancelled` / `client_disconnected` (real-server test and unit tests) |
| Never leave incomplete messages marked completed | [x] | Conditional terminal writes; shielded finalization; one streaming answer per conversation (partial unique index); stale-stream sweeper (beat, verified live) |
| Workspace access for read / send / delete / citations | [x] | 404 for other members (including admins), other workspaces and outsiders on all ten endpoints |

Acceptance criteria (verified 2026-11-24):

- [x] Conversation history persists.
- [x] Users can continue conversations.
- [x] AI responses stream correctly.
- [x] Citations are displayed (returned per message and per citation endpoint, with spoken labels).
- [x] Cross-workspace conversations are inaccessible.

Tests: 295 unit and 160 integration tests (backend), including two against a real uvicorn server
(mid-stream cancel, client disconnect); live end-to-end on the Compose stack (13/13 checks).

### Phase 6 follow-ups / known issues

- Conversations are private to their creator (ADR 0016). Sharing and admin compliance access are a
  product decision for Phase 8.
- A disconnect cancels the answer rather than finishing it in the background; resumable streams
  (`Last-Event-ID`) are not implemented.
- Follow-up condensation is verified with scripted models and the fallback only; its quality with the
  real model is unmeasured (needs the OpenAI key; see Phase 5).
- If an API process crashes mid-answer, that conversation rejects new questions (409) until the
  answer is marked stale (5 minutes by default).
- No chat UI yet (Phase 7).

## Phase 6b: Voice and accessible reading ✅ (ADR 0011, ADR 0018)

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Ask by voice (speech-to-text → normal conversation pipeline, same authorization) | [x] | Mic button and Ctrl+Shift+Space; browser or DocuMind engine; e2e: dictated question → review → answer |
| Answers read aloud, sentence by sentence, with spoken sources | [x] | Listen per answer; auto-read while streaming plus "Sources. 1: …"; unit tests for marker handling |
| Document reading mode: read page/section, next/previous, summarize, ask in context | [x] | Viewer listening mode with sentence highlight; `POST /documents/{id}/summary` (page range, cited, isolated) |
| Opt-in voice, never auto-play; pause/stop shortcuts; adjustable speed | [x] | Off by default; Escape, Ctrl+Shift+Space, Ctrl+Shift+↑/↓; reading bar; speed 0.5–2× |
| Spoken confirmation for destructive actions | [x] | Confirm and 2FA dialogs announce themselves when voice is on (e2e: archive) |
| Per-workspace voice policy; audio not stored by default | [x] | Migration 0008; `GET/PUT /voice`, audited; 403 when restricted; tests assert nothing is persisted |
| Speech provider adapters | [x] | OpenAI STT/TTS behind the Phase 5 ports (mocked HTTP tests); browser fallback |
| Figure and chart descriptions (deferred from Phase 5) | [x] | PDF raster figures → vision description → searchable, cited (integration test); off by default |
| Testing sessions with blind and low-vision users | [ ] | Pending (Phase 9) |

Tests: backend 309 unit and 179 integration; frontend 78 unit; Playwright 4 end-to-end (voice journey uses
simulated speech APIs; the rest is the real stack).

### Phase 6b follow-ups / known issues

- **Not verified against the real OpenAI speech or vision APIs** (no key or network here); adapters are
  tested against mocked HTTP. Browser speech was exercised with simulated speech APIs in headless Chromium.
- Testing with blind and low-vision users, and with NVDA, JAWS and VoiceOver, is still required (Phase 9).
- Audio minutes and vision calls aren't metered yet (Phase 8 usage tracking).
- Vector-drawn charts (not images) aren't described; DOCX images aren't described yet.
- Push-to-talk only (no continuous conversation mode).

## Phase 7: Frontend implementation ✅

| Requirement | Status | Evidence |
|-------------|--------|----------|
| 16 pages: landing, login, registration, forgot password, dashboard, library, upload, viewer, collections, chat, history, organization, members, settings, profile, usage | [x] | Every page is exercised by the Playwright journey or unit tests; plus verify-email, reset-password, accept-invitation and 404 |
| Navigation: workspace switcher, sidebar, user menu, document search, quick upload, new conversation | [x] | `features/shell/AppShell.tsx`; journey switches workspaces, uses the menus and the mobile drawer |
| Chat: document selection, input, streaming, Markdown, citation cards, source navigation, history | [x] | Journey: stream, follow-up, regenerate, "Open source" → viewer with the excerpt highlighted |
| Feature-based architecture, reusable components, TanStack Query | [x] | `src/features/*`, `src/components/ui` |
| Axios interceptors, typed API contracts | [x] | Single-flight refresh, 401 retry, CSRF header (`client.test.ts`); generated `schema.d.ts` with a backend staleness test |
| Protected routes, error boundaries, skeletons, toasts | [x] | `RequireAuth` (route tests and e2e), safe `next=` redirects, per-route error boundary, `Loading`, live-region toasts |
| Empty, loading and error states; confirmation dialogs | [x] | All list pages; `useConfirm` for archive/delete/remove/leave; 2FA step-up dialog for permanent deletion |
| Responsive layouts | [x] | 390 px journey: drawer navigation, no horizontal overflow on documents, chat, conversations, collections, profile, settings |
| Accessibility and keyboard navigation (ADR 0011) | [~] | axe (WCAG 2.2 A/AA) clean on login, dashboard, documents, chat, conversations, members and usage; WAI-ARIA menus and dialogs; focus management; spoken citation labels. **Manual NVDA/JAWS/VoiceOver testing is still pending (Phase 9).** |
| Backend support | [x] | `GET /workspaces/{id}/usage` (usage.read) for the usage dashboard |

Acceptance criterion (verified 2026-11-24): **the complete user journey works without Swagger or manual API
calls.** The Playwright journey covers:

- registration, email verification (link from Mailpit) and sign-in;
- upload, processing and the viewer;
- a streamed answer with citations, a follow-up, regenerate, and source navigation;
- conversation history;
- creating an organization, inviting a colleague (who signs up in a second browser and accepts) and role-restricted UI;
- usage, the audit log, and 2FA setup with recovery codes;
- permanent deletion with a 2FA code, and sign-out followed by a protected-route redirect.

Tests: 62 frontend unit and component tests (Vitest); 3 Playwright end-to-end tests on the live stack; backend
296 unit and 162 integration tests.

### Phase 7 follow-ups / known issues

- Manual screen-reader testing (NVDA, JAWS, VoiceOver) and testing with blind users (ADR 0011) are pending.
- The frontend Docker image wasn't rebuilt here (Docker Hub rate-limited the base-image lookup with 429).
  The app was verified with the same Vite dev server run natively against the containerized backend.
  `npm ci` in the image picks up the new dependencies from the updated lockfile.
- The UI's role → permission map mirrors the backend; keep them in sync (UI only; the API enforces).
- Very long documents render every page in the viewer (no virtualization yet).
- End-to-end runs use real sign-ups, so repeated local runs hit the auth rate limits; raise
  `RATE_LIMIT_AUTH_PER_IP` for the dev stack or clear the Redis `rl:*` keys.
- The landing page's scenario companies are illustrative (see the landing notes above).

## Phase 8: Usage, administration and SaaS readiness

- [ ] Usage tracking, quotas, plans/entitlements model, audit log views
- [ ] Audio-minute and figure-description usage (ADR 0011); the per-workspace voice policy shipped in Phase 6b
- [ ] **Bring your own API key (ADR 0019, added 2026-11-24 at the product owner's request)**
  - [ ] Workspace-level provider keys (personal workspace owner; organization owners and admins), OpenAI first
  - [ ] Encrypted at rest (AES-GCM, separate key, bound to the workspace), write-only, never logged; last 4 characters shown
  - [ ] 2FA step-up to add or replace a key; validation call on save; audit events
  - [ ] Per-workspace key resolution in API and worker; "use my key only" (no platform fallback)
  - [ ] Clear errors when a key is revoked or out of quota (jobs fail with `provider_key_invalid`, admin banner and email)
  - [ ] Usage records marked `billed_to = workspace_key`, excluded from platform quotas
  - [ ] Re-index flow when the provider or embedding model changes
  - [ ] Later: Azure OpenAI / OpenAI-compatible endpoints behind a hostname allowlist (SSRF protection)

## Phase 9: Security, testing and performance

- [ ] Security review, Playwright E2E, load tests on the 4 vCPU / 8 GB target
- [ ] Accessibility audit and testing with blind users (ADR 0011)
- [ ] BYOK review: key storage and rotation, leak paths (logs, errors, backups), SSRF on custom endpoints (ADR 0019)

## Phase 10: Production deployment

- [ ] Production Compose, Nginx + TLS, backups, runbooks, security checklist
- [ ] Resolve ADR 0006 (production object storage)
