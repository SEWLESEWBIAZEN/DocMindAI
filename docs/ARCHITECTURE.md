# DocuMind AI: Architecture

This document describes the system as it exists now and the shape it is
designed to grow into. Decisions with trade-offs are recorded as ADRs in
[`docs/adr`](adr/README.md).

## 1. System context

```
            Browser (React SPA)
                    │  same-origin /api/*
                    ▼
     Nginx (Phase 10) / Vite dev proxy (development)
                    │
                    ▼
           FastAPI backend (documind) ─────────────► OpenAI (Phase 4+, via provider port)
             │          │           │
             ▼          ▼           ▼
       PostgreSQL     Redis      S3-compatible storage
       + pgvector   (cache,      (MinIO locally)
                    Celery broker)
                        ▲
                        │
             Celery workers (Phase 4)
```

The browser only talks to the backend, always same-origin under `/api`.
Storage credentials never reach the frontend. Downloads will use short-lived
pre-signed URLs (Phase 3).

## 2. Backend layering

The backend lives in `backend/src/documind` and is organised in layers with
dependencies pointing inward:

```
api  ──►  application  ──►  domain  ◄──  infrastructure
 │                                            ▲
 └──────────── container.py (composition root) ┘
```

| Layer            | Responsibility                                                                 | May import                         |
| ---------------- | ------------------------------------------------------------------------------ | ---------------------------------- |
| `domain`         | Business concepts and **ports** (Protocols). No framework or I/O imports.      | stdlib only                        |
| `application`    | Use-case services that orchestrate domain logic through ports.                 | `domain`                           |
| `infrastructure` | **Adapters** that implement ports: SQLAlchemy, Redis, boto3, later OpenAI.     | `domain`, `core`, third-party libs |
| `api`            | FastAPI routes, request/response schemas, dependencies, middleware.            | `application`, `domain`, `core`    |
| `core`           | Cross-cutting: configuration, logging.                                         | stdlib, pydantic                   |
| `container.py`   | The **only** place concrete adapters are instantiated and wired into services. | everything                         |

**Dependency inversion in practice (Phase 1 example).** `domain/health.py`
defines the `HealthCheck` port. `infrastructure/{database,cache,storage}/health.py`
implement it. `application/health_service.py` depends only on the port, and
`container.py` wires the concrete checks in. Unit tests substitute fakes without
touching any infrastructure.

**Request flow.** Routes obtain services via FastAPI dependencies
(`api/dependencies.py`), which read the `Container` that the app lifespan stores
on `app.state`. Nothing uses module-level singletons for I/O resources. That
makes tests able to inject a different container (`create_app(container_factory=...)`).

### Planned modules (later phases)

Phase 8 added: `application/ai` (resolver, metering, BYOK), `application/billing`, `application/credentials`,
`application/infrastructure`, `domain/billing`, `domain/ai`, `domain/infrastructure`, `infrastructure/ai`,
`infrastructure/network` (SSRF policy), `infrastructure/database/dataplane*.py` and `provisioning.py`.

The same structure will hold the product features. For example:

- `domain/identity`, `application/identity`, `infrastructure/database/models/identity.py` (Phase 2)
- `domain/storage.ObjectStorageProvider` port + `infrastructure/storage/s3_provider.py` (Phase 3)
- `application/processing/*` orchestrator + Celery tasks in `infrastructure/tasks` (Phase 4)
- `domain/rag` + `application/rag` retrieval and answers, `LLMProvider` port (Phase 5, done)
- `application/conversations` with SSE streaming (Phase 6, done)

Tenant isolation will be enforced in application services and in repository
queries (workspace ID is a mandatory filter parameter, never optional), not
only in routes. Phase 2 establishes this pattern.

## 3. Identity, tenancy and authorization (Phase 2)

See ADR 0008 (sessions), ADR 0009 (tenancy and permissions) and ADR 0010
(persistence layering).

```
users ──< workspace_members >── workspaces ──1:1── organizations
  │              │                  │ (kind = personal → personal_owner_id = user)
  │            roles ──< role_permissions >── permissions
  ├──< refresh_tokens   (hashed, rotated, grouped by family_id)
  ├──< account_tokens   (hashed; email_verification | password_reset)
  └──< invitations      (workspace, email, role; hashed token)
audit_logs (append-only; no FKs so history survives deletions)
```

**Request authorization path for workspace routes**

1. `get_current_user` decodes the bearer JWT and loads the user. The token's
   `ver` claim must match `users.auth_version`.
2. `get_workspace_context(workspace_id)` loads the caller's membership. If
   there is none it raises 404. Otherwise it returns a `WorkspaceContext` with
   the role and the permissions from `role_permissions`.
3. The service calls `ctx.require(Permission.…)` and applies the hierarchy and
   ownership policies.
4. Repositories filter by `ctx.workspace_id` in SQL. Rows read and then changed
   under an invariant are locked `FOR UPDATE`.

**API surface** (all under `/api/v1`; full schema at `/api/docs`)

| Area | Endpoints |
| ---- | --------- |
| Auth | `POST /auth/register`, `/auth/verify-email`, `/auth/verify-email/resend`, `/auth/login`, `/auth/refresh`*, `/auth/logout`*, `/auth/logout-all`, `/auth/password/forgot`, `/auth/password/reset` |
| Me | `GET/PATCH /me`, `POST /me/password`, `GET /me/workspaces`, `PUT /me/active-workspace`, `GET /me/invitations`, `POST /me/invitations/{id}/accept`, `POST /me/invitations/{id}/decline` |
| Organizations | `POST /organizations` |
| Workspace | `GET/PATCH /workspaces/{id}`, `POST /workspaces/{id}/leave`, `GET /workspaces/{id}/members`, `PATCH/DELETE /workspaces/{id}/members/{member_id}`, `GET/POST /workspaces/{id}/invitations`, `DELETE /workspaces/{id}/invitations/{invitation_id}`, `GET /workspaces/{id}/audit-logs` |
| Invitations | `POST /invitations/accept` (emailed token) |

\* Authenticated by the refresh cookie; requires the `X-CSRF-Protection: 1` header.

Errors are JSON of the form `{"detail": "...", "code": "..."}`, where `code`
is stable for clients:

| Status | Meaning |
| ------ | ------- |
| 401 | Not authenticated or invalid credentials |
| 403 | Permission denied, email not verified, or CSRF header missing |
| 404 | Not found, including resources in other tenants |
| 409 | Conflict: last owner, personal workspace, already a member |
| 400 | Invalid or expired one-time token |
| 422 | Validation failed |
| 429 | Rate limited (`Retry-After` header) |

## 4. Documents and storage (Phase 3)

See ADR 0012.

```
workspaces ──< documents ──< document_versions   (immutable; storage_key per version)
                   │ (id, workspace_id) composite FKs enforce same-tenant links
                   └──< document_collection_items >── document_collections
```

**Upload pipeline** (`POST /workspaces/{id}/documents`, multipart):

1. Body-size middleware.
2. Permission check (`documents.write`).
3. In a worker thread: size check, filename sanitization, signature and
   structure validation, SHA-256.
4. Malware scan.
5. Duplicate check.
6. Put the object at `workspaces/{ws}/documents/{doc}/versions/{ver}`.
7. Commit the rows, with status `pending`. If the commit fails, delete the object.
8. Audit event.

**Download**: `GET …/download-url` returns a 5-minute signed GET URL for the
public storage endpoint, forcing an attachment and a safe content type.

| Area | Endpoints (under `/api/v1/workspaces/{workspace_id}`) |
| ---- | ----------------------------------------------------- |
| Documents | `POST/GET /documents` (upload; list and search by `q`, `status`, `format`, `collection_id`, `uploaded_by`, with sort and pagination), `GET/PATCH/DELETE /documents/{id}`, `GET /documents/{id}/download-url`, `GET/POST /documents/{id}/versions` |
| Collections | `GET/POST /collections`, `GET/PATCH/DELETE /collections/{id}`, `PUT/DELETE /collections/{id}/documents/{document_id}` |

**Archiving and deletion (ADR 0013).**

- Members archive or restore their own uploads; Admins and Owners can archive any.
- Archived documents are hidden and read-only.
- Permanent deletion needs `documents.delete` (Admin/Owner) **and** a 2FA
  step-up token less than 5 minutes old.

| Area | Endpoints |
| ---- | --------- |
| Archive | `POST …/documents/{id}/archive`, `POST …/documents/{id}/restore`, `GET …/documents?archived=exclude\|only\|include` |
| 2FA | `GET /me/mfa`, `POST /me/mfa/totp/setup`, `POST /me/mfa/totp/confirm`, `POST /me/mfa/disable`, `POST /me/mfa/recovery-codes`, `POST /auth/mfa/verify` (step-up → elevated access token) |

Errors added: 403 `mfa_enrollment_required`, `mfa_step_up_required`;
401 `invalid_mfa_code`; 409 `document_archived`, `mfa_already_enabled`.

Errors added: 413 `payload_too_large`, 415 `unsupported_file` /
`malware_detected`, 409 `duplicate_document` / `duplicate_collection`,
503 `service_unavailable` (scanner down).

## 5. Document processing (Phase 4)

See ADR 0014.

```
upload tx ─┬─ documents (status=pending)
           └─ processing_jobs (queued)  ──after commit──► Celery "documents" queue
                                                              │
Celery beat ── sweep every 30 s: re-dispatch lost jobs,       ▼
               recover expired leases, create missing jobs   worker: claim (atomic) →
                                                              extract (PyMuPDF / OCR / python-docx / text)
                                                              → normalize → chunk (page- & section-aware)
                                                              → embed (batched, de-duplicated)
                                                              → replace pages + chunks (one tx) → ready
```

| Endpoint (under `/api/v1/workspaces/{id}/documents/{doc}`) | Purpose |
| --- | --- |
| `GET /processing` | Status, warnings, job history with stats |
| `POST /reprocess` | Queue again (409 while a job is active) |
| `GET /pages` | Extracted text per page (OCR flag, headings) |
| `GET /chunks` | Indexed chunks with page and heading metadata |

## 6. Search and grounded answers (Phase 5)

See ADR 0015.

```
POST /search ─┐
POST /ask ────┴─► RetrievalService ── embed query (EmbeddingProvider)
                     │   validate document/collection filters (404 if not in workspace)
                     ├─► vector:  pgvector HNSW cosine, iterative scan  ┐  tenant + ready + not archived
                     └─► keyword: tsvector GIN, websearch_to_tsquery     ┘  + current version, all in SQL
                              └──► reciprocal-rank fusion ─► top-k chunks
/ask:  build_context (numbered, budgeted, injection-neutralized <source> blocks)
       → PromptManager (versioned system prompt) → LLMProvider (OpenAI | local)
       → resolve_citations (verify [n], drop invalid, excerpt, visual + spoken labels)
       → { answer, status: grounded | insufficient_context, citations[], usage, model, prompt_version }
```

| Module | Role |
| --- | --- |
| `domain/rag` | Pure logic: models, prompt text, context builder, RRF fusion, citation resolver |
| `application/rag/retrieval.py` | Query validation, filter checks, hybrid retrieval |
| `application/rag/orchestrator.py` | `answer()` and `stream()` (sentinel-safe streaming for Phase 6 SSE) |
| `infrastructure/database/repositories/retrieval.py` | The tenant-filtered SQL |
| `infrastructure/llm` | OpenAI Chat Completions adapter; offline extractive stand-in |

Speech ports (`SpeechToTextProvider`, `TextToSpeechProvider`) live in
`application/ports.py`; adapters ship in Phase 6b (ADR 0011).

## 7. Conversations and streaming (Phase 6)

See ADR 0016.

```
POST …/conversations/{id}/messages            (Accept: text/event-stream → SSE, else JSON)
  request tx: lock conversation → fail stale answers → 409 if one is streaming
              → condense follow-up → retrieve (conversation scope) → build context
              → commit user message (completed) + assistant message (streaming)
  stream:     producer task: LLM stream ──queue──► consumer (HTTP response)
                                                 ├─ answer.delta events, keep-alive comments
                                                 ├─ polls Redis cancel flag (POST …/cancel)
                                                 └─ heartbeat → messages.updated_at
  end:        own short tx → completed (+ citations) | cancelled | failed   (shielded)
  recovery:   beat task sweep_stale_replies → failed/interrupted after 5 min without heartbeat
```

| Module | Role |
| --- | --- |
| `application/conversations/service.py` | CRUD, sources, history, send, regenerate, cancel, access rule |
| `application/conversations/streaming.py` | `ReplyRunner`: producer/consumer, cancellation, heartbeats, terminal state |
| `application/conversations/finalizer.py` | Conditional terminal writes in fresh sessions |
| `application/rag/condense.py` | Follow-up to standalone query (LLM with deterministic fallback) |
| `api/sse.py` | SSE event encoding |
| `infrastructure/cache/cancellation.py` | Redis cancel flags |

### Voice and accessible reading (Phase 6b)

See ADR 0018. The browser holds a single `speaker` queue (browser `speechSynthesis` or
`POST /voice/speech`) and dictation (`SpeechRecognition` or `MediaRecorder` → `POST /voice/transcribe`);
`VoiceProvider` combines user preferences with the workspace voice policy (`GET/PUT /voice`) and owns
shortcuts and the reading bar. Answers are spoken with their sources; the viewer's listening mode reads
page by page and can summarize a page range (`POST /documents/{id}/summary`). PDFs can carry figure
descriptions produced during processing (`ImageDescriber`, `application/processing/figures.py`).

## 7b. Plans, usage and your own AI keys (Phase 8, ADR 0019, 0020, 0022)

* **AI resolution.** Services never use a global AI provider. `AIResolver.resolve(workspace_id, user_id)`
  (`application/ai/resolver.py`) returns a `WorkspaceAI` with one adapter per capability (embeddings,
  chat, vision, speech-to-text, text-to-speech), chosen as follows:
  1. The workspace's own key, if its provider offers the capability. If DocuMind marked that key
     invalid, the capability is unavailable: there is no silent fallback.
  2. Otherwise DocuMind's platform key, if the plan allows platform AI and "use my key only" is off.
  3. Otherwise unavailable (`provider_key_required`).

  The API (`get_workspace_ai`) and the worker (`ResolvedProcessingAI`) use the same resolver.
* **Metering.** Every adapter handed out is wrapped by a `Metered*` adapter that writes one
  `usage_records` row per successful call. Each row records `billed_to`, the workspace, the user (none for
  background work) and the source (`usage_source(...)`: message, processing job). Cancelled streams record
  an estimate. Speech records seconds (reported, or estimated from size or characters).
* **Quotas.** `Quotas` (`api/dependencies.py`) runs admission checks: uploads (documents, storage,
  platform embedding tokens), answers and summaries (chat tokens), speech (audio seconds), invitations
  (members). Figure descriptions are capped by the remaining quota inside processing.
* **BYOK.** Keys are stored by the credential vault (`application/credentials/vault.py`) using envelope
  encryption. `SqlWorkspaceKeys` decrypts a key only in memory and builds guarded adapters (cached for 5
  minutes). A provider rejection (auth error or out of quota) marks the key `invalid`, audits it, emails
  owners and admins, and fails with `provider_key_invalid`; processing jobs then fail permanently. Saving
  a key requires a 2FA step-up and a validation call. If the embedding model changes, the save requires
  confirming a re-index (`409 reindex_required`).

## 7c. Infrastructure independence and data planes (Phase 8, ADR 0021)

```
              control plane (DocuMind PostgreSQL)                  data planes
  users · workspaces · members · audit · plans · usage   ┌──► shared   (same database, public schema)
  credentials · AI keys · infrastructure registry ───────┼──► dedicated (DocuMind server, one DB per org)
                                                          └──► external  (customer PostgreSQL, documind schema)
  documents · versions · collections · pages · chunks · jobs · conversations · messages · citations
```

* **Profiles.** `infrastructure_profile_versions` are immutable. Each holds a non-secret database,
  storage and processing configuration, with credential references and a SHA-256 checksum. A workspace
  is pinned to one active version (`workspace_infrastructure`); no row means the platform defaults.
* **Resolver.** `InfrastructureResolver.resolve(workspace_id)` (`application/infrastructure/resolver.py`)
  verifies the checksum, then builds the data-plane engine (small pool, `search_path=documind,public`)
  and the storage adapter (customer bucket under a prefix). Both are cached per version, with LRU
  eviction.
* **Routing.**
  * `get_data_session` hands data-plane services either the request's own session (shared) or a session
    on the workspace's database. Audit entries in isolated planes go to `audit_outbox` in the same
    transaction, and are relayed to `audit_logs` idempotently (by `event_id`) after the request and on
    every sweep.
  * Jobs are dispatched with their workspace id. The worker's `InfrastructureTargets` picks the data
    plane and storage for each job.
  * Sweeps iterate every active data plane.
* **Schema.** `dataplane_metadata()` derives the data-plane schema from the ORM models:
  * workspace references point to `tenant_workspaces`;
  * user references are plain UUIDs.

  `alembic_dataplane` migrates it. A parity test compares it with the shared schema.
* **Lifecycle.** A version goes from draft, to validation, to activation.
  * Validation checks connectivity, the SSRF policy, PostgreSQL 14+, pgvector, a non-superuser role and
    the schema revision, plus a storage probe and a public-bucket check.
  * Activation requires a 2FA step-up. It is refused with `migration_required` (and a planned
    `infrastructure_migrations` row) when a workspace with data would change its database or storage
    location.
  * Dedicated databases are provisioned at draft time (`DedicatedProvisioner`).
* **Storage providers:** S3, S3-compatible, MinIO and Azure Blob (MI-6). All are adapters behind
  `ObjectStorageProvider`.
* **Customer-hosted processing (MI-7, ADR 0024).**
  * `documind-worker` runs in the customer's network and claims jobs in the customer's database.
  * It only heartbeats and reports usage to `/worker/v1/*`.
  * DocuMind's sweeper never hands customer-mode jobs to its own workers.
* **Approved migrations (MI-8, ADR 0025).**
  * An owner approves a planned migration with a 2FA step-up.
  * `MigrationExecutor` then:
    1. locks the workspace (the resolver raises 503 `migration_in_progress`);
    2. checks that the destination is empty;
    3. copies rows in dependency order, and objects key by key, through the ports;
    4. verifies counts and sizes;
    5. switches the pin.
  * The source is kept until an explicit, audited cleanup. A failure removes only what the run
    copied.

## 8. Runtime resources and lifecycle

`create_app()` builds the app. The lifespan:

1. Builds the `Container`: async SQLAlchemy engine (pooled, `pool_pre_ping`),
   async Redis client (pooled), boto3 S3 client. All of them connect lazily.
2. Starts serving. **The API starts even if a dependency is down.** Readiness
   reports it, and the dependency recovers without a restart.
3. On shutdown, closes Redis, disposes the engine pool and closes the S3 client.

## 9. Health model

| Endpoint                   | Purpose   | Touches dependencies | Failure                                                         |
| -------------------------- | --------- | -------------------- | --------------------------------------------------------------- |
| `GET /api/v1/health/live`  | Liveness  | No                   | n/a. Used by container health checks so a DB outage does not restart the API |
| `GET /api/v1/health`       | Readiness | Yes                  | 503 with per-component status                                   |

Checks run concurrently, each bounded by `HEALTH_CHECK_TIMEOUT_SECONDS`. The
database check also asserts that the `vector` extension is installed. Raw
exception messages are logged but never returned; clients see either a
curated message (`DependencyCheckError`) or the exception type. In
production, details and error reasons are hidden from the response.

## 10. Data and migrations

- SQLAlchemy 2.x async with asyncpg. All models share `infrastructure/database/base.Base`
  with a deterministic constraint naming convention.
- Alembic reads `DATABASE_URL` from settings (never from `alembic.ini`).
  `alembic -x database_url=...` targets another database, which the tests use.
- pgvector is created both by the Postgres first-boot init script (as
  superuser) and by migration `0001` (idempotent). This supports managed
  databases where no init script runs.

## 11. Observability

- Structured logs via the standard library: JSON in production
  (`LOG_FORMAT=json`), human-readable in development.
- `RequestContextMiddleware` (pure ASGI, so it is safe for future SSE streaming)
  assigns or propagates `X-Request-ID`, binds it to a context variable included
  in every log line, logs one access line per request (health probes at DEBUG),
  and converts unhandled exceptions into a generic JSON 500 that carries the
  request ID.
- Client-supplied request IDs are accepted only if they match
  `[A-Za-z0-9._-]{1,128}`, which prevents log injection.

## 12. Frontend

See ADR 0007 (toolchain) and ADR 0017 (application architecture).

```
/ (landing) · /status · /login /register /forgot-password /reset-password /verify-email
/invitations/accept (signed in)
/app → /w/:workspaceId/dashboard
/w/:workspaceId/  dashboard · documents[/:id] · upload · collections[/:id]
                  chat/:conversationId? · conversations · members · organization
                  settings · profile · usage
```

| Concern | Implementation |
| --- | --- |
| Session | Access token in memory; HttpOnly refresh cookie; silent refresh on load; one shared refresh on 401 (`lib/api/session.ts`, `client.ts`) |
| Contract | `lib/api/openapi.json` + generated `schema.d.ts` (`make api-types`); a backend test rejects a stale contract |
| Server state | TanStack Query, keyed per workspace (`['ws', id, …]`); polling while documents process |
| Streaming | `fetch` + SSE parser (`lib/api/sse.ts`); stop = cancel endpoint; leaving = abort |
| UI kit | `components/ui`: Button, Field, Dialog/useConfirm, Toast, Menu, Loading/Empty/Error states, Pagination |
| Shell | Sidebar (workspace switcher, navigation, recent conversations), top bar (document search, quick upload, user menu), upload tray, mobile drawer |
| Tests | Vitest + Testing Library (unit and component); Playwright + axe end-to-end journey (`make e2e`) |

## 13. Environments

| Environment   | Config source                              | Notes                                                        |
| ------------- | ------------------------------------------ | ------------------------------------------------------------ |
| development   | `.env` (from `.env.example`) + Compose     | Docs enabled, console logs, all ports on 127.0.0.1            |
| test          | Explicit `Settings(...)` in unit tests; `.env` for integration | Unit tests never read `.env`                 |
| production    | `deploy/production/.env` + production Compose | Placeholder credentials and wildcard CORS rejected at startup; docs off by default |

## 14. Deployment target

The reference target is a single Linux VM (4 vCPU / 8 GB RAM) running `deploy/production/`:

```
internet ─► web (Nginx: TLS, CSP and headers, rate limits, SSE unbuffered, SPA) ─► api (uvicorn)
                                                                           │
             certbot (webroot renewals)         worker, beat (Celery) ─────┤
                                                                           ▼
             internal network: redis, postgres (optional)    egress: S3, SMTP, AI, customer DBs
             backup (nightly, encrypted, offsite)
```

- Application containers run as non-root, with a read-only root filesystem and `cap_drop: ALL`.
- Only `web` publishes ports.
- `migrate` runs Alembic, the bucket bootstrap and `dataplanes migrate` before each release.
- Prometheus scrapes `/api/v1/metrics` with a bearer token on the internal network
  (`deploy/monitoring/`).

Other deployment models (hybrid, customer-managed cloud, on-premises) are in
[`DEPLOYMENT_MODELS.md`](DEPLOYMENT_MODELS.md). Operations are in [`runbooks/`](runbooks/).
