# ADR 0005: Structured stdlib logging, request IDs, liveness vs readiness

- Status: Accepted
- Date: 2026-11-23

## Context

Operators need machine-parseable logs that correlate across a request, and
orchestrators need health signals that do not cause restart storms when a
dependency fails.

## Decision

- **Standard-library logging** with a small JSON formatter (production) and a
  console formatter (development). Uvicorn loggers are routed through the same
  handler. Its access log is replaced by our middleware.
- **`RequestContextMiddleware`** (pure ASGI, so it is compatible with SSE
  streaming) assigns or propagates a validated `X-Request-ID`, binds it to a
  `ContextVar` that is attached to every record, logs one line per request, and
  converts unhandled exceptions into a generic JSON 500 carrying the request ID.
- **Liveness** (`/api/v1/health/live`) never touches dependencies. Container
  health checks use it.
- **Readiness** (`/api/v1/health`) checks PostgreSQL (and pgvector), Redis and
  the storage bucket concurrently with per-check timeouts. It returns 503 when
  any check fails. Raw exception text is logged, never returned. Production
  hides versions and error reasons.

## Alternatives

- *structlog:* richer API, but an extra dependency for what roughly 100 lines
  of stdlib code achieve. It can be adopted later without changing call sites
  much.
- *Starlette `BaseHTTPMiddleware`:* known problems with streaming responses
  and contextvars.
- *Single combined health endpoint used for liveness:* a database outage would
  then restart every API container, which amplifies incidents.

## Consequences

- Log fields passed via `extra=` become JSON keys. Avoid putting secrets or
  document content into `extra`.
- Metrics and tracing (OpenTelemetry) are deferred to Phase 9.
