# DocuMind AI

A multi-tenant AI document intelligence platform: upload documents to a personal
or organization workspace, then search them semantically and hold cited,
grounded conversations across them.

> **Status: Phases 1–10 complete** (manual screen-reader and blind-user testing, and a
> penetration test, still pending). The full product works in the browser:
> sign-up and 2FA, personal and organization workspaces with roles,
> uploads with OCR and indexing, search, and conversations with streamed,
> page-cited answers that link back to the highlighted source passage.
> Phase 8 added plans, metered usage and quotas, bring-your-own AI key, and
> per-organization infrastructure: a dedicated or customer-run PostgreSQL and
> the customer's own S3-compatible storage. Phase 9 added a security review with
> fixes, encrypted backups with a tested restore, accessibility and load tests.
> Phase 10 added the production stack (Nginx, TLS, hardened containers, backups,
> monitoring), runbooks, customer-hosted workers, Azure Blob storage and approved
> data migrations between infrastructures. See
> [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md).

**Accessibility is a product goal.** DocuMind is being designed for blind
and low-vision professionals too: full screen-reader support, questions by
voice, answers read aloud with spoken citations, and a listening mode for
studying documents. See [ADR 0011](docs/adr/0011-voice-and-accessibility.md).

## Stack

| Layer            | Technology                                                       |
| ---------------- | ---------------------------------------------------------------- |
| Backend API      | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async), Alembic |
| Database         | PostgreSQL 16 + pgvector 0.8                                     |
| Cache / broker   | Redis 7.4 (Celery broker from Phase 4)                           |
| Object storage   | S3-compatible via boto3; MinIO in development                    |
| Frontend         | React 19, Vite, TypeScript, TanStack Query, Axios                |
| Tooling          | uv + `uv.lock`, Ruff, mypy (strict), pytest, Vitest, oxlint      |
| Local runtime    | Docker Compose                                                   |

## Repository layout

```
backend/                 FastAPI service (package: documind)
  src/documind/
    api/                 HTTP layer: routes, dependencies, middleware
    application/         Use-case services (orchestrate domain + ports)
    domain/              Framework-free models and ports (interfaces)
    infrastructure/      Adapters: database, cache, storage
    core/                Configuration and logging
    container.py         Composition root (wires adapters to services)
    main.py              App factory; asgi.py is the ASGI entry point
    cli.py               `documind-manage` operational commands
  alembic/               Database migrations
  tests/unit, tests/integration
frontend/                React + Vite SPA (feature-based: src/features/*)
infra/postgres/init/     First-boot database initialisation
docs/                    Architecture, configuration, ADRs, status
docker-compose.yml       Development stack
```

## Quick start (Docker)

Prerequisites: Docker with Compose v2.

```bash
cp .env.example .env          # development placeholders only
docker compose up --build
```

| URL                                 | What                                      |
| ----------------------------------- | ----------------------------------------- |
| http://localhost:5173               | DocuMind: landing page, then **Create account** to use the app |
| http://localhost:5173/status        | System status page                        |
| http://localhost:8000/api/v1/health | Readiness: app + dependency health        |
| http://localhost:8000/api/docs      | OpenAPI docs (disabled in production)     |
| http://localhost:9001               | MinIO console (credentials from `.env`)   |
| http://localhost:8025               | Mailpit: every email the app sends (verification, reset, invitations) |

Background services: `worker` (Celery; processes uploads) and `beat` (runs the
recovery sweep). In development, embeddings and answers use offline `local`
providers, so no API key is needed. They match words rather than meaning, and the
local "model" only quotes matching sentences. For real results set
`EMBEDDING_PROVIDER=openai`, `LLM_PROVIDER=openai` and `OPENAI_API_KEY` (then run
`documind-manage reindex` so existing chunks get real embeddings).

The backend container applies migrations and creates the private storage
bucket on start. All ports are bound to `127.0.0.1`.

## Native development

Prerequisites: [uv](https://docs.astral.sh/uv/) ≥ 0.11, Node.js ≥ 22.12, Docker.

```bash
make setup        # uv sync + npm ci (creates .env if missing)
make infra        # PostgreSQL, Redis, MinIO in Docker
make bootstrap    # alembic upgrade head + create bucket
make backend-dev  # API on :8000 with reload
make frontend-dev # Vite on :5173, proxies /api to :8000
```

`make help` lists every target.

## Using the app

1. Open http://localhost:5173 and choose **Create account**. The verification email is in Mailpit
   (http://localhost:8025).
2. Upload PDFs, Word, text or Markdown files (**Upload** in the top bar). They show **Ready** once indexed.
3. Choose **New conversation** and ask a question. Answers stream in with numbered citations; **Open source**
   jumps to the highlighted passage in the document.
4. Create an organization from the workspace switcher, invite colleagues, and manage roles under **Members**.
5. Turn on two-factor authentication under **Profile**. Admins need it to delete documents permanently.
6. For voice, open **Profile → Voice and reading** and turn on voice features: ask by voice, hear answers
   with their sources, and use **Listen** in a document to have it read aloud (Escape stops,
   Ctrl+Shift+Space pauses). Browser speech works out of the box; set `SPEECH_PROVIDER=openai` for
   DocuMind voices. Admins choose what each workspace allows under **Settings → Voice**.
7. **Usage** shows the workspace's plan and monthly quotas. Operators assign plans with
   `docker compose exec backend documind-manage plan assign <workspace_id> business`.
8. **AI providers** (owners and admins) lets a workspace use its own OpenAI key. The key is encrypted,
   never shown again and protected by 2FA, and the workspace can choose to use its own key only.
9. **Infrastructure** (organization owners, Business or Enterprise plan) moves an organization to a
   dedicated database, its own PostgreSQL, or its own S3-compatible bucket. For dedicated databases in
   development, set `DEDICATED_DATABASE_ADMIN_URL=postgresql+asyncpg://documind:<password>@postgres:5432/postgres`
   in `.env`.

## Testing and quality

```bash
make lint              # ruff, mypy --strict, oxlint, tsc
make test              # backend unit tests + frontend tests
make test-integration  # backend tests against real Postgres/Redis/MinIO
make e2e               # Playwright journey + axe accessibility scans (needs the running stack)
make api-types         # regenerate the frontend's typed API contract after API changes
```

Load tests: `loadtest/README.md`. Backups: `infra/backup/backup.sh` and `restore.sh`, see
`docs/runbooks/backup-and-restore.md`.

Backend integration tests are opt-in (`pytest -m integration`) and require
`make infra bootstrap`. They use a separate `<db>_test` database for migration
round-trips.

## Trying the identity API

With the stack running, open http://localhost:8000/api/docs:

1. `POST /api/v1/auth/register` with an email, a password (at least 10 characters) and a display name.
2. Open Mailpit at http://localhost:8025, copy the token from the verification
   link, and call `POST /api/v1/auth/verify-email`.
3. `POST /api/v1/auth/login` returns an access token. Click **Authorize** and paste it.
4. Try `POST /api/v1/organizations`, invitations, members and `GET /api/v1/me/workspaces`.

## Trying search and answers

After signing in (above), using your personal workspace id from `GET /api/v1/me/workspaces`:

1. `POST /api/v1/workspaces/{id}/documents` with a PDF, DOCX, TXT or Markdown file. Wait for
   `status: "ready"` (`GET …/documents/{doc}`).
2. `POST /api/v1/workspaces/{id}/search` with `{"query": "renewal notice"}` returns ranked passages
   with pages and sections.
3. `POST /api/v1/workspaces/{id}/ask` with `{"question": "How much notice is needed?"}` returns an
   answer with `[n]` markers and matching `citations` (document, page, excerpt, plus a
   `spoken_label` for screen readers). If the documents don't contain the answer, `status` is
   `insufficient_context`.

Both accept `document_ids`, `collection_ids` and `top_k`. They only ever see the workspace's
ready, non-archived documents.

## Trying conversations

1. `POST /api/v1/workspaces/{id}/conversations` with `{}` (whole workspace) or
   `{"document_ids": [...], "collection_ids": [...]}`.
2. `POST …/conversations/{cid}/messages` with `{"content": "..."}`. Add the header
   `Accept: text/event-stream` to stream, for example
   `curl -N -H "Accept: text/event-stream" -H "Authorization: Bearer …" -H "Content-Type: application/json" -d '{"content":"How much notice is needed?"}' http://localhost:8000/api/v1/workspaces/{id}/conversations/{cid}/messages`.
   You get `message.created`, `answer.delta` events, then `answer.completed` with citations.
3. Ask follow-ups in the same conversation; `GET …/messages` returns the history.
4. `POST …/messages/{mid}/cancel` stops an answer; `POST …/messages/{mid}/regenerate` re-answers the
   latest question.

Conversations are private to the member who created them.

## Health endpoints

- `GET /api/v1/health/live`: liveness. Always 200 while the process runs; it never touches dependencies.
- `GET /api/v1/health`: readiness. 200 when PostgreSQL (including pgvector),
  Redis and the storage bucket are all reachable, otherwise 503 with
  per-component status. In production only statuses are shown, not versions
  or error reasons.
- `documind-manage check` runs the same checks from the command line.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Configuration reference](docs/CONFIGURATION.md)
- [Architecture decision records](docs/adr/README.md)
- [Implementation status](docs/IMPLEMENTATION_STATUS.md)
- [Deployment models](docs/DEPLOYMENT_MODELS.md): managed, hybrid, customer cloud, on-premises
- [Production checklist](docs/PRODUCTION_CHECKLIST.md)
- Runbooks: [deployment, upgrade and rollback](docs/runbooks/deployment.md),
  [incident response](docs/runbooks/incident-response.md),
  [disaster recovery and key rotation](docs/runbooks/disaster-recovery.md),
  [backup and restore](docs/runbooks/backup-and-restore.md)
- [Customer-hosted worker](deploy/customer-worker/README.md)

## Security notes

- No credentials are committed. `.env` is git-ignored; `.env.example` contains
  development placeholders that the backend rejects when `ENVIRONMENT=production`.
- The storage bucket is private. Bootstrap refuses to proceed if a bucket policy is attached.
- Unhandled errors return a generic JSON body with a request ID. Stack traces are never sent to clients.
