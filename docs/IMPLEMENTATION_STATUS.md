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

- [x] Require 2FA at sign-in for users who enabled it; required 2FA on paid plans and for
      organizations that turn it on, with a grace period (ADR 0026, `test_mfa_sign_in.py`,
      `test_mfa_enforcement.py`, Playwright journey)
- [x] Self-service recovery of a lost authenticator: sign in and turn 2FA off with one-time codes by
      email (ADR 0027, `test_mfa_email_codes.py`, Playwright journey)
- [x] Support-assisted 2FA reset for users who lose every factor and their email: reviewed by
      support, 72-hour waiting period, cancelled by any real 2FA use (ADR 0030,
      `test_mfa_support_reset.py`, `documind-manage mfa-reset`)
- [x] Email one-time codes as a fallback factor (ADR 0027); 24-hour block after a password reset
- [x] Email-only 2FA for people who can't use an authenticator app; organizations can accept or
      refuse email codes; recovery codes can be regenerated with an email code (ADR 0028,
      `test_mfa_email_method.py`, `e2e/mfa-email.spec.ts`)
- [x] Passkeys (WebAuthn) as a second factor: phishing-resistant, accepted everywhere (ADR 0029,
      `test_passkeys.py` with a software authenticator, `e2e/passkeys.spec.ts` with Chromium's
      virtual authenticator)
- [ ] Passwordless (usernameless) sign-in with passkeys
- [x] Frontend 2FA screens (QR code, manual secret, recovery codes): built in Phase 7
      (`features/admin/pages.tsx`, Security section); covered by the Playwright journey

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

## Phase 8: SaaS readiness and infrastructure independence ✅ (MI-7/MI-8 foundations only)

Combines the original Phase 8 with the approved multi-infrastructure workstreams MI-1 to MI-5, and lays the
foundations for MI-6 to MI-8. Impact assessment: [`PHASE_8_IMPACT_ASSESSMENT.md`](PHASE_8_IMPACT_ASSESSMENT.md).
ADRs: 0020 (plans, usage, quotas), 0021 (infrastructure profiles and data planes), 0022 (credential vault), 0019 (BYOK).

### 8A. Usage, quotas, plans and entitlements (original Phase 8)

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Plan catalog and entitlements; per-workspace plan with overrides; operator assignment (audited) | [x] | `domain/billing/plans.py`, migration 0009 (seed matches catalog, `test_billing.py`); `documind-manage plan list/assign`; `workspace.plan_assigned` audit (`test_chat_quota_blocks_answers_with_402`) |
| Usage ledger (idempotent) for embeddings, chat, summaries, condensation | [x] | `usage_records` + `Metered*` adapters; worker indexing and API answers recorded with user, source and `billed_to` (`test_processing_and_answers_are_metered_to_the_platform`); no document content (`test_usage_records_hold_no_document_content`) |
| Audio-minute usage | [x] | STT seconds (reported or estimated) and TTS characters → seconds (`test_server_speech_is_metered_and_capped_by_audio_quota`) |
| Figure-description usage | [x] | Vision calls metered; descriptions stop at the remaining monthly quota with a `figures_not_described:quota` warning (unit test) |
| Quotas at admission, `402 quota_exceeded` | [x] | Documents, storage, members (incl. pending invitations), chat tokens, embedding tokens, audio seconds (integration tests) |
| Usage and plan views (API and UI), audit log views | [x] | `GET /plan`; Usage page plan panel with accessible meters (Playwright `saas.spec.ts`, axe clean); audit log panel from Phase 7 |

### 8B. Bring your own API key (ADR 0019, 0022)

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Credential vault: envelope encryption, key ring, rewrap rotation, write-only | [x] | `infrastructure/security/vault.py`, `application/credentials/vault.py`; `test_vault.py`; rotation end to end (`test_vault_key_rotation_rewrap`); revoked ciphertext destroyed |
| Workspace-level provider credentials (OpenAI; provider-neutral) | [x] | `workspace_ai_providers` (provider + settings + capabilities); catalog with planned providers refused (`test_planned_providers_are_refused`) |
| 2FA-protected credential changes; audit events | [x] | Add/replace/remove and turning "key only" off need a step-up; audit `ai_provider.*` without secrets (`test_adding_a_key_needs_2fa_validation_and_reindex_confirmation`) |
| Provider validation on save (sanitized errors) | [x] | Model retrieval per required model; key never echoed (`test_validation_errors_are_sanitized`); 422 bodies no longer echo inputs |
| Per-workspace, per-capability resolution in API and worker | [x] | `AIResolver`; worker decrypts with the same vault (`factory.state["secrets"]`); unsupported capability falls back only if allowed (`test_billing.py`) |
| No silent platform-key fallback; "use my key only" | [x] | `test_rejected_key_fails_clearly_and_never_falls_back`, `test_key_only_blocks_the_platform` |
| Provider usage attribution (`billed_to`), excluded from platform quotas | [x] | `test_workspace_key_usage_is_attributed_and_outside_platform_quotas` |
| Clear errors for invalid/revoked keys | [x] | `provider_key_invalid` (503) with a clear message; key marked invalid, admins emailed, jobs fail permanently, revalidation restores it |
| Re-indexing when the embedding configuration changes | [x] | `409 reindex_required` → confirm → jobs queued; `GET/POST /reindex`; per-workspace `documind-manage reindex` |
| Documents on an old embedding model are visible, not silently skipped | [x] | `needs_reindex` badge, "Re-index all" banner, `documents_need_reindex` warning on search and answers (ADR 0031; `test_documents_on_another_model_are_reported_not_silently_skipped`) |
| Pricing tiers (Free, Pro $19, Business $99 per member/month, yearly = 2 months free, Enterprise by quote) with custom storage from Pro and dedicated/external databases from Business | [x] | `/pricing`, `GET /plans`, ADR 0032 (`test_pricing.py`, `test_public_catalog_lists_prices_and_infrastructure`) |
| Self-serve billing: Stripe Checkout, portal, signed idempotent webhooks, seat sync, plan changes guarded by infrastructure in use | [x] | `test_billing_api.py` (6), `e2e/billing.spec.ts`; runbook `runbooks/billing.md` |
| 14-day free Pro trial without a card (once per workspace and per person), trial-ending email | [x] | ADR 0033; `test_free_pro_trial_once_per_workspace_and_person` |
| Onboarding: sample documents (load, suggested questions, one-click removal), getting-started checklist | [x] | ADR 0033; `test_onboarding_api.py` (4), `e2e/onboarding.spec.ts` |
| Structured extraction: templates (obligations and deadlines, contract terms, invoice) and custom fields; every value cited; CSV export | [x] | ADR 0034; `test_extraction.py` (9), `test_extraction_api.py` (5), `e2e/extraction.spec.ts` |
| Side-by-side comparison of documents, with differing fields marked | [x] | Fields view in `features/extraction/pages.tsx`; `test_compare_contract_terms_and_limits` |
| Tracked deadlines: email reminders (7/1/0 days by default), calendar file (.ics) | [x] | `test_deadlines_reminders_and_calendar` |
| Google Drive connector: OAuth (PKCE, browser-bound callback, 2FA to connect or disconnect), folder picker, first sync and change feed, new versions, archive on removal, quotas, hourly/15-min sync | [x] | ADR 0035; `test_connectors_api.py` (3), `test_google_drive.py` (4), `e2e/connectors.spec.ts`. Not yet run against live Google from this sandbox. |
| Single sign-on (OIDC) for Business and Enterprise organizations: PKCE, nonce, browser-bound callback, JWKS-verified ID tokens, operator-verified domains, just-in-time provisioning, optional enforcement | [x] | ADR 0036; `test_sso_api.py` (4), `e2e/access.spec.ts`. Tested with a fake provider, not a live IdP. |
| Google Workspace SSO: Workspace accounts only (`hd` claim), fixed issuer and setup steps in the settings; SSO sessions end after 12 h without sliding | [x] | ADR 0036; `test_google_workspace_accounts_only_and_sessions_dont_slide`, `e2e/access.spec.ts`. Google discovery and keys checked live; no sign-in with a real Google account yet. |
| SCIM 2.0 user provisioning and deprovisioning (token shown once, last owner protected) | [x] | ADR 0036; `test_scim_provisioning` |
| Per-collection access: restricted collections hidden from non-members in lists, search, answers, summaries, extraction and deadlines | [x] | ADR 0036; `test_collection_access_api.py` (2), `e2e/access.spec.ts` |
| Workspace API keys (Pro and up): scopes, expiry, shown once and stored hashed, 2FA to create, limited to document/search/answer routes, never admin permissions | [x] | ADR 0037; `test_keys_are_scoped_to_their_workspace_routes_and_scopes`, `test_integrations.py`, `e2e/integrations.spec.ts` |
| MCP server (Streamable HTTP): search, ask, list and read documents, list collections, with the same permissions and quotas | [x] | ADR 0037; `test_mcp_server_tools`; checked with the official MCP Python SDK |
| Slack app: signed requests, install (2FA), account linking by code, private slash-command answers, in-thread answers from unrestricted documents only | [x] | ADR 0037; `test_slack_api.py` (2). Fake Slack API; not yet run against a live Slack workspace. |
| Available on every plan (entitlement) | [x] | `byok=True` on all plans (`test_byok_is_on_every_plan`) |
| AI providers page | [x] | `features/saas/AIProvidersPage.tsx` (Playwright, axe clean, 390 px) |

### 8C. Infrastructure independence (MI-1 to MI-5)

| Requirement | Status | Evidence |
|-------------|--------|----------|
| MI-1: registry, immutable versioned profiles with checksums, pinning, resolver, provider factories | [x] | Migration 0011; `domain/infrastructure/profiles.py`; `InfrastructureResolver`; tampering → 503 `infrastructure_integrity_error` (`test_tampered_profile_is_refused`) |
| MI-2: storage providers (AWS S3, S3-compatible, MinIO) with validation and SSRF policy | [~] | Customer MinIO bucket with prefix end to end incl. signed downloads (`test_external_database_and_customer_storage`); probe + public-bucket check; SSRF tests. **Real AWS S3 not exercised here** (same boto3 adapter) |
| MI-3: dedicated PostgreSQL | [x] | `DedicatedProvisioner` (database + least-privilege role + pgvector + migrations), verified in tests and inside the backend container; two organizations isolated (`test_dedicated_databases_isolate_organizations`) |
| MI-4: external PostgreSQL | [x] | Customer database with a non-superuser role; validation (SSRF, version, superuser, pgvector, schema revision, consent to migrate); TLS modes, `verify-full` enforced in production |
| Data-plane isolation | [x] | Tenant anchor FKs (foreign workspace insert rejected), audit outbox relayed exactly once, schema parity test, multi-plane sweeps, jobs carry `workspace_id` |
| MI-5: organization infrastructure settings UI | [x] | `features/saas/InfrastructurePage.tsx`: drafts, validation results, 2FA activation, migration plans, workers, history (Playwright, axe clean) |
| Existing data untouched | [x] | Workspaces without a profile keep the shared database and platform bucket; activation with data returns `migration_required` and moves nothing (`test_moving_a_workspace_with_data_needs_a_migration`) |

### 8D. Foundations for MI-6 to MI-8

| Requirement | Status | Evidence |
|-------------|--------|----------|
| MI-6: Azure Blob registered as planned | [x] | Listed, not selectable; rejected by parsing (unit test) |
| MI-7: processing modes, worker registrations (hashed tokens, revocation), queue routing | [~] | Registration, SHA-256 tokens shown once, revocation (`test_customer_worker_tokens_are_hashed_and_revocable`); customer mode fails validation with `not_available`. **Connector protocol and worker package not built (Phase 9–10)** |
| MI-8: health events, migration state model | [~] | `infrastructure_events`, `infrastructure_migrations` (planned/cancelled). **No migration execution, no periodic health checks, no deprovisioning yet** |

Acceptance (verified 2026-11-24):

- Backend: 367 unit and 200 integration tests (real PostgreSQL, Redis and MinIO), all passing, including
  13 infrastructure, 8 BYOK and 7 quota tests. Lint, format and `mypy --strict` are clean.
  Both Alembic environments upgrade, downgrade and pass `alembic check`.
- Frontend: 82 Vitest tests. Playwright 6/6, including the Phase 7 journey and the new Phase 8 spec, with
  axe clean and no horizontal scrolling at 390 px.
- Live: the dev Compose stack migrated to 0011. A dedicated database was provisioned and validated from
  inside the backend container.

> **Update (Phase 10):** the MI-6, MI-7 and MI-8 rows above describe the state at the end of Phase 8.
> All three were completed in Phase 10; see that section. The rows are kept for history.

### Phase 8 follow-ups / known issues

- **Not verified against live OpenAI** (no key or network here). BYOK validation and adapters are tested
  with mocked HTTP and a fake provider factory.
- **Real AWS S3 not exercised.** MinIO stands in, with the same boto3 adapter. Azure Blob is planned
  (MI-6).
- **MI-7 connector and MI-8 execution are not implemented.** Customer processing can't be activated, and
  migrations are planned but never run. Dedicated databases aren't deprovisioned automatically (there is
  no organization deletion yet).
- Plan, AI-key and infrastructure changes made in another process apply within
  `TENANT_SETTINGS_CACHE_SECONDS` (10 s).
- Quotas can be exceeded by about one request under concurrency (ADR 0020). Speech seconds are estimates
  when the provider doesn't report them.
- External databases use the system CA bundle; uploading a custom CA isn't supported yet. Signed download
  URLs point at the customer's storage endpoint, so browsers must be able to reach it.
- ~~There is no payment processing: plans are assigned by operators.~~ Stripe self-serve billing was added later (ADR 0032).
- The backend Docker image must be rebuilt to include `alembic_dataplane` (the dev Compose file mounts it).

## Phase 9: Security, testing and performance

Report: [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md). Load tests: [`loadtest/README.md`](../loadtest/README.md).
Backups: [`runbooks/backup-and-restore.md`](runbooks/backup-and-restore.md). Accessibility:
[`accessibility/`](accessibility/).

### Original scope

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Full security review | [x] | `SECURITY_REVIEW.md`: 8 findings fixed (headers, 422 echo, log redaction, outbound probe limit, DNS re-check, outage handling, secret rotation, contrast); dependency audits clean (pip-audit, npm audit). **An independent penetration test is still recommended** |
| Playwright end-to-end testing (extended to Phase 8) | [x] | `journey`, `voice`, `saas`, `a11y` specs (8 tests) on the live stack |
| Load tests on the 4 vCPU / 8 GB target | [x] | Compose limits (8 GB, 4 vCPU split); about 135 requests/s with 0 errors at 25 and 50 busy users; answer p95 under 0.9 s; uploads ready p95 1.6 s (`loadtest/README.md`). Local AI providers: OpenAI latency not included |
| Accessibility audit | [x] | axe WCAG 2.2 A/AA on every page in light **and** dark mode; keyboard-only journey (skip link, menus, dialogs, composer, focus indicators); 1 contrast issue fixed |
| Manual NVDA, JAWS and VoiceOver verification | [ ] | Protocol ready (`accessibility/screen-reader-test-protocol.md`); **needs testers with real screen readers** |
| Testing with blind and low-vision users | [ ] | Study plan ready (`accessibility/user-testing-plan.md`); **needs recruited participants** |
| BYOK review: storage, rotation, leakage (logs, errors, backups), SSRF | [x] | Vault rotation (rewrap) and revocation; no key in logs, responses, audit or `pg_dump` (`test_secrets_never_leak_*`); SSRF policy with re-check at connect |
| Backup security and restoration tests | [x] | Encrypted, authenticated archives (tampering, truncation and wrong passphrase rejected); full restore drill into a fresh database and bucket, signed in and used (`test_backup_restore.py`) |

### Multi-infrastructure verification

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Shared and dedicated database isolation | [x] | Two dedicated databases, tenant anchor FKs, cross-organization 404s (`test_dedicated_databases_isolate_organizations`, `test_organizations_cannot_reach_each_others_documents`) |
| External database security | [x] | Non-superuser check, TLS modes (`verify-full` in production), SSRF, consent to manage the schema, password rotation |
| Storage provider isolation | [x] | Customer bucket under a prefix, signed URLs for that bucket, other organizations get 404 |
| Worker authentication and revocation | [~] | `POST /worker/v1/heartbeat` authenticates hashed tokens; wrong, unknown, user and revoked tokens get 401. **The job protocol (MI-7) is not built yet** |
| Infrastructure profile integrity; configuration version pinning | [x] | Checksums verified on load (`test_tampered_profile_is_refused`); immutable versions; jobs resolve the pinned version |
| Secret rotation | [x] | Credential key ring rewrap; database password rotation without migration; old credential revoked |
| Infrastructure migration safety | [~] | Moving data is refused and planned, never run (`migration_required`). **Execution (MI-8) not built** |
| Customer network security | [x] | SSRF policy (private, loopback, link-local, metadata, CGNAT, IPv4-mapped); outbound validation rate limit |
| Infrastructure failure behaviour | [x] | Customer database outage: 503 for that organization only; others and identity unaffected; jobs left for the sweeper |

> **Update (Phase 10):** the worker job protocol (MI-7) and migration execution (MI-8) were built in
> Phase 10. The two `[~]` rows above are now covered there.

Tests at the end of Phase 9 (2026-11-24): backend 390 unit and 206 integration tests (real PostgreSQL, Redis and MinIO); frontend 82 Vitest; Playwright 8/8 (journey, voice, Phase 8, accessibility). Lint, format and `mypy --strict` clean.

## Phase 10: Production deployment ✅

Deployment: [`runbooks/deployment.md`](runbooks/deployment.md).
Checklist: [`PRODUCTION_CHECKLIST.md`](PRODUCTION_CHECKLIST.md).
Models: [`DEPLOYMENT_MODELS.md`](DEPLOYMENT_MODELS.md).
ADRs: 0023 (production storage; closes 0006), 0024 (customer processing), 0025 (migrations and Azure).

### Original scope

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Production Docker Compose and Nginx reverse proxy | [x] | `deploy/production/docker-compose.yml`. Services: `web`, `api`, `worker`, `beat`, `redis`, optional `postgres`, `migrate`, `certbot` and `backup`. Internal, egress and public networks; non-root, read-only, `cap_drop: ALL`. The smoke run (`deploy/production/smoke/run.sh`) started the stack in production mode, and the Playwright journey and Phase 8 specs passed **5/5 over HTTPS**. It also found two real image bugs, now fixed and guarded by a build-time import check: the Alembic config path, and `kombu` needing `packaging` |
| TLS and certificate renewal | [~] | Nginx: TLS 1.2/1.3, HSTS, CSP and the other headers, unbuffered SSE, rate limits, `/api/v1/metrics` blocked; a temporary self-signed certificate until certbot (webroot) succeeds; reload every 6 h. Verified with a local CA in the smoke run. **No real Let's Encrypt issuance here** (it needs a public DNS name) |
| Database backups | [x] | The `backup` profile: `schedule.sh` runs nightly encrypted archives (control plane, shared plane and dedicated planes), an offsite upload with write-only credentials and a success metric. Restore drill: `test_backup_restore.py`. Run-once mode for pre-upgrade backups |
| Object storage backups and recovery | [x] | `documind-manage objects export/import` with a manifest and path checks, included in the archive (`test_backup_restore.py`) |
| Deployment and rollback runbooks | [x] | `runbooks/deployment.md` (first deployment, upgrade including data planes, rollback, routine operations), `incident-response.md` (one section per alert), `disaster-recovery.md` (scenarios, RPO and RTO, key rotation), `backup-and-restore.md` |
| Production security checklist | [x] | `PRODUCTION_CHECKLIST.md`. Commands were checked against the stack (header names, profiles, entrypoints) |
| Production object storage decision (closes ADR 0006) | [x] | ADR 0023: any S3-compatible managed service, with a private bucket and MinIO for on-premises. Azure Blob is per organization (ADR 0025) |
| Production observability | [x] | `GET /api/v1/metrics` (bearer `METRICS_TOKEN`, 404 when unset; `test_operations.py`); Prometheus configuration and 8 alert rules (`deploy/monitoring/`); JSON logs with redaction and request ids |

### Multi-infrastructure deployment

| Requirement | Status | Evidence |
|-------------|--------|----------|
| DocuMind-managed, hybrid and customer-managed cloud deployments; on-premises documentation | [x] | `DEPLOYMENT_MODELS.md`, with the plan entitlements each mode needs. On-premises uses the same production stack. **Air-gapped operation with a local model server is untested** |
| Customer worker deployment package (MI-7) | [x] | `documind-worker` (image target `customer-worker`), `deploy/customer-worker/` (Compose, env, README); heartbeat and usage API; embedding-model contract; revocation exits with code 3 (`test_customer_worker.py`, `test_customer_worker_tokens_are_hashed_and_revocable`). Customer and hybrid modes validate once a worker has checked in |
| MI-6 Azure Blob adapter | [x] | `azure_provider.py` (account key, read-only SAS downloads with attachment disposition, public-container refusal). Tested against **Azurite** (`test_azure_storage.py`, 3 tests); Azurite is in Compose (profile `azure`) and CI. **Not tested against real Azure**; Entra ID authentication not implemented |
| MI-8 migration execution | [x] | ADR 0025: approval with 2FA; lock (503 `migration_in_progress`); empty-destination preflight; copy, verify and switch; retained source and audited cleanup; abort for dead runs (`test_infrastructure_migration.py`, 3 tests; plus S3 to Azure in `test_azure_storage.py`) |
| Disaster recovery procedures | [x] | `runbooks/disaster-recovery.md` covers host, database, bucket, dedicated plane, customer infrastructure and region loss, and key rotation for every secret |
| Production provider configuration | [x] | `deploy/production/.env.example`, `generate-secrets.sh`, and the `CONFIGURATION.md` sections for production, worker and monitoring; production validation rejects unsafe settings |
| Operational monitoring and runbooks | [x] | Every alert links to a runbook section that exists |
| **Future releases: more BYOK providers** (Azure OpenAI, Anthropic, Google, Mistral, OpenAI-compatible and local models) | [ ] | Not in this phase. Each will sit behind the existing ports, with custom endpoints only via the SSRF policy |

Tests at the end of Phase 10 (2026-11-24), all run locally:

| Suite | Result |
|---|---|
| Backend unit | 394 passed |
| Backend integration (real PostgreSQL, Redis, MinIO and Azurite) | 215 passed |
| Frontend Vitest | 82 passed |
| Playwright on the dev stack (journey, voice, Phase 8, accessibility) | 8/8 passed |
| Production smoke over HTTPS (P10.1) | 5/5 passed |

`ruff`, `mypy --strict`, `oxlint` and `tsc` are clean.

### Phase 10 follow-ups / known issues

- **GitHub Actions doesn't run.** Every workflow run fails within seconds without logs; this looks
  like an account or billing problem, not the workflow. CI has therefore not been verified on
  GitHub; all results above come from local runs.
- **Images were not rebuilt and published from this environment.** Docker Hub rate-limited base
  image pulls, so the smoke run mounted the source over the previous runtime image
  (`SMOKE_USE_SOURCE=1`). The dev image was rebuilt from cache. Build and scan the release images
  in CI before deploying.
- **Not exercised here:** real Let's Encrypt issuance, real AWS S3, real Azure, live OpenAI.
- **Migrations** run in one process and do not resume. A dead run needs
  `documind-manage migrations abort`. The workspace is unavailable while a migration runs.
- **No re-encryption tool for `MFA_ENCRYPTION_KEY`**: rotating it means users re-enrol in 2FA.
  There is no operator tool to move a dedicated data plane to another host.
- **Still open from Phase 9:** manual screen-reader tests, sessions with blind and low-vision
  users, and an independent penetration test.
