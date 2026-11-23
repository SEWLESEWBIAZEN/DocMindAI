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
- In organizations, Members can delete any document; consider uploader- or admin-only.
- Production Nginx must set `client_max_body_size` to match `MAX_UPLOAD_BYTES` (Phase 10).
- A filename containing `"` is received as `%22` (browsers encode it in multipart per the
  HTML spec); it is displayed as sent.

## Phase 4: Document processing engine

- [ ] Celery + Redis; reliable dispatch (outbox or equivalent)
- [ ] Extractor (PyMuPDF, python-docx, text), normaliser, chunker
- [ ] **OCR for scanned PDFs (required, ADR 0011)**; preserve reading order, headings and page numbers
- [ ] Text descriptions for figures and charts (vision model; ADR 0011)
- [ ] Embedding provider abstraction (OpenAI), batching, retries, dedup
- [ ] pgvector repository; idempotency; retries; failed-job handling

## Phase 5: AI search and RAG engine

- [ ] Query embedding, vector search with in-query tenant filtering, hybrid search
- [ ] Context builder, prompt manager, LLM provider abstraction, citation resolver
- [ ] `SpeechToTextProvider` / `TextToSpeechProvider` ports; speakable citation labels (ADR 0011)
- [ ] Grounded answers with page-level citations; insufficient-context handling

## Phase 6: Conversation and chat system

- [ ] Conversation, ConversationDocument, Message, MessageCitation
- [ ] SSE streaming with cancellation and disconnect handling

## Phase 6b: Voice and accessible reading (new, ADR 0011)

- [ ] Ask questions by voice (speech-to-text → existing RAG pipeline, same authorization)
- [ ] Answers read aloud, streamed sentence by sentence, with spoken citations
- [ ] Document reading mode: read page or section, next/previous, summarize, ask in context
- [ ] Opt-in voice, never auto-play over screen readers; pause/stop shortcuts; adjustable speed
- [ ] Spoken confirmation for destructive actions
- [ ] Per-workspace voice policy; audio not stored by default
- [ ] Testing sessions with blind and low-vision users

## Phase 7: Frontend implementation

- [~] **Landing page** (delivered early at the product owner's request, 2026-11-23)
  - Animated hero product demo (typed question → streamed answer → citation-linked source highlight),
    cycling through five team scenarios; benefits bento, use-case tabs, pipeline, security, CTA
  - Motion: CSS transitions/animations on `transform`/`opacity` with shared easing tokens; hover gated
    to fine pointers; reduced motion shows the finished demo state and keeps fades only
  - Accessibility: skip link, ARIA tabs with arrow-key navigation, labelled demo region, focus styles
  - Verified: 28 frontend tests (timeline logic, tabs, reduced motion, autoplay, routes); Playwright
    screenshots in light, dark and 390 px mobile with no horizontal overflow
  - Scenario companies, documents and figures are fictional and labelled illustrative; replace with
    approved customer stories before using any as testimonials
  - `/login` and `/register` show a "coming soon" page until authentication ships (Phase 2)
  - Routing is a minimal pathname switch (`src/app/routes.tsx`); a full router comes with the app shell
- [ ] Remaining pages (login, registration, dashboard, library, chat, etc.), navigation shell, chat UI
- [ ] **Accessibility acceptance (ADR 0011):** WCAG 2.2 AA, full keyboard operation, polite live regions
      for streamed answers, citation links, heading and page navigation; axe checks plus NVDA, JAWS
      and VoiceOver testing

## Phase 8: Usage, administration and SaaS readiness

- [ ] Usage tracking, quotas, plans/entitlements model, audit log views
- [ ] Audio-minute usage; per-workspace voice policy (ADR 0011)

## Phase 9: Security, testing and performance

- [ ] Security review, Playwright E2E, load tests on the 4 vCPU / 8 GB target
- [ ] Accessibility audit and testing with blind users (ADR 0011)

## Phase 10: Production deployment

- [ ] Production Compose, Nginx + TLS, backups, runbooks, security checklist
- [ ] Resolve ADR 0006 (production object storage)
