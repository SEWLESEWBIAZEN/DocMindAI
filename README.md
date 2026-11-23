# DocuMind AI

A multi-tenant AI document intelligence platform: upload documents to a personal
or organization workspace, then search them semantically and hold cited,
grounded conversations across them.

> **Status: Phase 1 (foundation) complete.** The repository has the
> infrastructure, backend and frontend skeletons with health checks. Product
> features (identity, documents, RAG, chat) arrive in later phases. See
> [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md).

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
| http://localhost:5173               | Frontend (system status page)             |
| http://localhost:8000/api/v1/health | Readiness: app + dependency health        |
| http://localhost:8000/api/docs      | OpenAPI docs (disabled in production)     |
| http://localhost:9001               | MinIO console (credentials from `.env`)   |

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
