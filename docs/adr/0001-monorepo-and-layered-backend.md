# ADR 0001: Monorepo and layered (ports-and-adapters) backend

- Status: Accepted
- Date: 2026-11-23

## Context

DocuMind AI has a Python API, background workers (Phase 4), and a React SPA.
They share contracts (API schemas) and release together. The backend will
integrate several replaceable external systems (PostgreSQL, Redis, S3, OpenAI)
and must enforce tenant isolation consistently.

## Decision

1. **Single repository** with `backend/`, `frontend/`, `infra/`, `docs/`, and
   one development `docker-compose.yml` at the root.
2. **Backend layers:** `domain` (framework-free models and ports),
   `application` (use-case services), `infrastructure` (adapters), `api`
   (FastAPI), `core` (config and logging). Dependencies point inward.
   `container.py` is the single composition root.
3. **Dependency injection** through FastAPI `Depends` reading a `Container`
   stored on `app.state` by the lifespan. No module-level I/O singletons.
4. **Workers** (Phase 4) will live in the same Python package and reuse the
   application and infrastructure layers. No separate service is created for them.

## Alternatives

- *Polyrepo:* adds cross-repo versioning overhead with no benefit at this size.
- *Flat FastAPI "routers + models" layout:* faster at the start, but business
  rules and tenant checks drift into route handlers, and swapping providers
  (an explicit requirement for LLM and storage) becomes invasive.
- *DI framework (e.g. dependency-injector):* unnecessary. One explicit
  composition root plus FastAPI `Depends` is enough and easier to read.

## Consequences

- Infrastructure can be faked in unit tests (see `tests/unit/test_health_api.py`).
- Slightly more files per feature. Accepted for clarity.
- The API process and workers can still be deployed and scaled independently
  even though they share code.
