# DocuMind AI

A multi-tenant AI document intelligence platform: upload documents to a personal
or organization workspace, then search them semantically and hold cited,
grounded conversations across them.

> **Status: Phases 1–6 complete.** Foundation, identity and tenancy, document
> management, the asynchronous processing pipeline (with OCR for scanned PDFs),
> AI search with grounded, page-cited answers, and persistent conversations with
> streamed (SSE), cancellable answers are in place. The app UI arrives in
> Phase 7. See
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
| http://localhost:5173               | Frontend landing page with live product demo |
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

## Testing and quality

```bash
make lint              # ruff, mypy --strict, oxlint, tsc
make test              # backend unit tests + frontend tests
make test-integration  # backend tests against real Postgres/Redis/MinIO
```

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

## Security notes

- No credentials are committed. `.env` is git-ignored; `.env.example` contains
  development placeholders that the backend rejects when `ENVIRONMENT=production`.
- The storage bucket is private. Bootstrap refuses to proceed if a bucket policy is attached.
- Unhandled errors return a generic JSON body with a request ID. Stack traces are never sent to clients.
