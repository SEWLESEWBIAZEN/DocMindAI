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

## Phase 2: Database and identity architecture

- [ ] Entities: User, Workspace, Organization, WorkspaceMember, Role, Permission, RefreshToken, AuditLog
- [ ] Registration (creates personal workspace), login, logout
- [ ] Argon2id password hashing
- [ ] Access tokens; refresh-token rotation and revocation
- [ ] Password reset; email verification
- [ ] Profile management
- [ ] Organizations, invitations, workspace switching, member management, ownership rules
- [ ] Roles Owner/Admin/Member/Viewer via explicit permission policies
- [ ] IDOR and cross-tenant protections; account-enumeration protection; auth rate limiting
- [ ] Alembic migrations; auth and authorization tests

## Phase 3: Document management and storage

- [ ] `ObjectStorageProvider` port + S3 (boto3) adapter
- [ ] Upload / list / metadata / download (signed URLs) / delete / rename
- [ ] Collections; metadata search; processing status
- [ ] PDF, DOCX, TXT, Markdown; MIME and signature validation; size limits
- [ ] Document, DocumentCollection, DocumentCollectionItem, DocumentVersion

## Phase 4: Document processing engine

- [ ] Celery + Redis; reliable dispatch (outbox or equivalent)
- [ ] Extractor (PyMuPDF, python-docx, text), normaliser, chunker
- [ ] Embedding provider abstraction (OpenAI), batching, retries, dedup
- [ ] pgvector repository; idempotency; retries; failed-job handling

## Phase 5: AI search and RAG engine

- [ ] Query embedding, vector search with in-query tenant filtering, hybrid search
- [ ] Context builder, prompt manager, LLM provider abstraction, citation resolver
- [ ] Grounded answers with page-level citations; insufficient-context handling

## Phase 6: Conversation and chat system

- [ ] Conversation, ConversationDocument, Message, MessageCitation
- [ ] SSE streaming with cancellation and disconnect handling

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

## Phase 8: Usage, administration and SaaS readiness

- [ ] Usage tracking, quotas, plans/entitlements model, audit log views

## Phase 9: Security, testing and performance

- [ ] Security review, Playwright E2E, load tests on the 4 vCPU / 8 GB target

## Phase 10: Production deployment

- [ ] Production Compose, Nginx + TLS, backups, runbooks, security checklist
- [ ] Resolve ADR 0006 (production object storage)
