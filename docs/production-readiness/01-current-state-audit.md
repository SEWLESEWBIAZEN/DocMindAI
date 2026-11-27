# 01 — Current-state audit

- Date: 2026-11-27
- Scope: the repository at commit `178e481`, the development stack in Docker Compose, and the
  production stack definition in `deploy/production`.
- Method: reading code, configuration and documentation, running the existing test suites,
  and probing every external service from the engineering environment.
- Not done: penetration testing, real production traffic, or anything on a live deployment
  (there is none).

CI/CD is out of scope. GitHub Actions fails because of an account billing issue, which is an
external item, not an application defect. All verification here runs locally.

## 1. Component inventory

| Component | Technology (actual versions) | Where |
|---|---|---|
| API | Python 3.13, FastAPI 0.165, Starlette 1.10, Uvicorn 0.54, Pydantic 2.14 | `backend/src/documind/api` |
| Domain and application services | Plain Python services with ports and adapters; dependency injection through `Container` and FastAPI `Depends` | `backend/src/documind/{domain,application}` |
| Persistence | PostgreSQL 16 with pgvector 0.8.7 (image `pgvector/pgvector:0.8.7-pg16`); SQLAlchemy 2.1 (async, asyncpg 0.32); Alembic 1.20 with two migration trees: control plane (`alembic/`, 23 revisions) and data plane (`alembic_dataplane/`, 5) | `backend/src/documind/infrastructure/database` |
| Background work | Celery 5.7 on Redis (queue `documents`); Celery beat for sweeps, syncs, reminders, seat sync and export clean-up | `backend/src/documind/worker` |
| Cache, rate limits, OAuth state | Redis 7.4 | `infrastructure/ratelimit`, SSO, Slack and Google state |
| Object storage | S3 API (boto3 1.44) against MinIO in development; Azure Blob adapter (azure-storage-blob 12.31) | `infrastructure/storage` |
| Document processing | PyMuPDF 1.28 (PDF), Tesseract OCR, python-docx 1.2, Markdown/text; optional ClamAV | `application/processing`, `infrastructure/extraction` |
| AI | OpenAI SDK 3.57 (chat, embeddings, vision, speech); Anthropic SDK 2.14 (chat only); local extractive provider for development | `infrastructure/{llm,embeddings,speech,ai}` |
| Identity | Argon2id passwords; HS256 JWT access tokens (15 min) plus rotating opaque refresh tokens in an HttpOnly cookie; TOTP, email codes and passkeys (webauthn 3.0); OIDC SSO; SCIM 2.0; workspace API keys | `application/identity`, `infrastructure/security` |
| Integrations | MCP server (Streamable HTTP), Slack app, Google Drive connector, Stripe billing, Google sign-in | `application/{integrations,connectors,billing}` |
| Frontend | React 19, React Router 7, TanStack Query 5, Vite 8, TypeScript 7 | `frontend/` |
| Edge | Nginx (TLS 1.2/1.3, HSTS, CSP), Certbot 3.0 (Let's Encrypt, webroot) | `deploy/production/nginx`, `certbot.sh` |
| Backups | `pg_dump` plus object export, encrypted archive (`documind-manage backup seal`), optional offsite S3 copy, scheduled container | `infra/backup`, `deploy/production` |
| Monitoring | `/api/v1/metrics` (Prometheus text, bearer token), 8 alert rules, JSON logs with request ids | `deploy/monitoring` |

**Tenancy model.**
- **Workspaces:** every user has a personal workspace and can belong to organization workspaces.
- **Authorization:** every request resolves a `WorkspaceContext` from the database (membership,
  role and permissions) for the workspace in the URL. A client-supplied workspace id is never
  trusted without that lookup.
- **Data:**
  - Data-plane rows carry `workspace_id`, and children reference parents through composite
    `(id, workspace_id)` foreign keys.
  - Retrieval filters by workspace and visibility in SQL.
  - Restricted collections (ADR 0036) narrow visibility further.
- **Isolation:** a workspace can be moved to a dedicated database or the customer's own
  database and bucket (MI-2 to MI-8).

## 2. External dependencies and their integration status

Statuses:
- **IMPLEMENTED:** the code exists.
- **MOCKED:** it is only exercised against a stand-in.
- **CONFIGURED:** the configuration exists but hasn't been tested.
- **VERIFIED:** it was tested against the real service.
- **BLOCKED:** verification needs unavailable credentials, access or infrastructure.
- **BROKEN:** a test found a defect.

"Initial" is the state at the start of this engagement. Phase 2 (`02-real-integration-verification.md`)
records what was then tested.

| Integration | Initial status | Evidence before this engagement | Network from this environment |
|---|---|---|---|
| OpenAI (platform key): chat, streaming, embeddings | IMPLEMENTED, MOCKED | Spy/fake LLMs in tests; an OpenAI stub in the production smoke test | Blocked at first; allowed after a dedicated environment was created |
| OpenAI vision, speech | IMPLEMENTED, MOCKED | Fake adapters | As above |
| OpenAI BYOK (workspace keys) | IMPLEMENTED, MOCKED | `validate()` tested with fakes | As above |
| Anthropic (platform chat) | IMPLEMENTED, MOCKED | Unit tests with a fake client | Reachable; no key |
| AWS S3 | IMPLEMENTED, MOCKED | Same boto3 adapter tested against MinIO, not AWS | Reachable (`s3.amazonaws.com`) |
| Azure Blob | IMPLEMENTED, MOCKED | 3 tests against Azurite | Blocked (`*.blob.core.windows.net`); no account |
| TLS / Let's Encrypt | CONFIGURED | Production smoke test with a self-signed CA on localhost | Let's Encrypt staging blocked at first, allowed after the change |
| Google sign-in, Google Workspace SSO (OIDC) | IMPLEMENTED, MOCKED | Fake IdP signing real RS256 tokens; Google discovery and JWKS fetched live | Reachable |
| SCIM 2.0 | IMPLEMENTED, MOCKED | Protocol-level tests | n/a (inbound) |
| Google Drive | IMPLEMENTED, MOCKED | Fake Drive API | Reachable (`www.googleapis.com`) |
| Stripe (test mode) | IMPLEMENTED, MOCKED | `FakeStripe` in tests. The owner reported a manual test-mode checkout and webhook working on their machine, but no record of it exists here | Blocked at first, allowed after the change |
| MCP | IMPLEMENTED, VERIFIED (local) | Integration tests, plus the official MCP Python SDK client against the local server | n/a |
| Slack | IMPLEMENTED, MOCKED | Fake Slack Web API; real signature algorithm | Blocked at first, allowed after the change |
| SMTP email | IMPLEMENTED, MOCKED | Mailpit | n/a |
| ClamAV | IMPLEMENTED, optional | Off by default | n/a |

## 3. Failure scenarios examined

| # | Scenario | Current behavior (from code) | Risk |
|---|---|---|---|
| F1 | Redis unavailable | `RedisRateLimiter.hit` catches `RedisError`, logs, and **allows the request**: every limit is off, including sign-in, password reset, 2FA codes and AI requests | High: brute force and cost abuse during an outage |
| F2 | User signs out on one device | The refresh-token family is revoked, but the **access token stays valid up to 15 minutes**. `logout_everywhere` bumps `auth_version`, which access tokens check; single-device logout doesn't | Medium |
| F3 | Infrastructure migration process dies mid-copy | Status stays `running`. The workspace stays locked (503) until an operator runs `migrations abort`, which deletes the partial copy. Nothing can be resumed, and there's no lease to stop two runners | Medium: a long outage for one workspace |
| F4 | MFA encryption key must change | One key, `MFA_ENCRYPTION_KEY`, encrypts TOTP secrets (AES-GCM, `v1:` format, no key id) and peppers recovery-code and email-code hashes. **No rotation path**: changing it locks out every 2FA user | High for operations |
| F5 | Worker killed during processing | The job lease (`PROCESSING_LEASE_SECONDS`, 30 min) expires, then the sweeper re-queues it (up to `max_attempts`, 5) or marks it dead | Low (exists and tested) |
| F6 | Object delete fails after the database commit | Logged as an orphan; **no clean-up job** | Low: storage cost and residual data |
| F7 | Upload: object written, then the database transaction fails | The object is deleted in `except`; if that delete fails too it's logged, again with no clean-up job | Low |
| F8 | PostgreSQL slow or hung query | `pool_pre_ping`, pool timeout; **no `statement_timeout`** | Low to medium |
| F9 | AI provider timeout or rate limit | SDK timeouts (`LLM_TIMEOUT_SECONDS` 60, embedding and speech timeouts); SDK retries bounded by `*_MAX_RETRIES`; errors mapped to `TransientProcessingError` and job retry | To verify against the real API |
| F10 | Configured model not available to the key | The key provided for verification lists only `text-embedding-3-small` and one chat model. The default `LLM_MODEL=gpt-5.4-mini` and the default speech models aren't available to it | Configuration risk: answers would fail until `LLM_MODEL` is set. Phase 2 checks the error is clean |
| F11 | Stripe webhooks late, duplicated or out of order | Signature check on the raw body; idempotent by event id; subscription state re-read from Stripe; duplicate-subscription guard; sync endpoint | Tested with fakes only |
| F12 | Database or storage unavailable at startup | Readiness `/api/v1/health` reports 503 per component; liveness `/health/live` doesn't touch dependencies | OK |

## 4. Reliability weaknesses (prioritized)

1. **F1, rate limiting fails open.** Fix in this engagement.
2. **F4, no 2FA key rotation.** Fix: a key ring and a re-encrypt command.
3. **F3, migrations can't resume.** Fix: a lease, step tracking and `resume`.
4. **F2, signed-out access tokens.** Fix: tie access tokens to their session.
5. **F6/F7, orphaned objects.** Fix: a reconciliation command that finds and removes objects no
   row refers to.
6. **F8, no statement timeout.** Fix: a configurable PostgreSQL `statement_timeout` for API
   connections.
7. **F10, model availability isn't checked at startup.** Document it, and report it through the
   readiness and `eval` tools.

## 5. Security observations

These are carried into `03-security-and-reliability-findings.md`.

- **Strong points, already tested:**
  - membership-checked workspace context on every request;
  - in-SQL tenant filters, and composite foreign keys across tenant tables;
  - CSRF header on cookie-authenticated refresh and logout;
  - an SSRF policy for every customer-supplied URL;
  - secrets in an envelope-encrypted vault with a key ring;
  - API keys stored hashed and limited by scope and route;
  - 2FA step-up for destructive actions;
  - production config validation that rejects weak secrets;
  - security headers and a strict CSP in Nginx.
- **Weak points:** F1, F2 and F4 above.
- **To check:** the dependency vulnerability status for this release, and whether logs ever
  contain tokens or secrets.

## 6. Missing operational controls

- **Orphaned objects:** no reconciliation.
- **Migrations:** no lease or heartbeat on runs.
- **Rate limiter:** no metric or log signal for degraded mode. There is a log line, but no
  metric or alert.
- **AI configuration:** no check that configured models exist for the configured key.
- **Backups:** restore is tested in an automated integration test (`test_encrypted_backup_and_full_restore`),
  but a timed restore drill on an isolated stack hasn't been recorded.

## 7. Prioritized remediation plan

| Priority | Item | Type | Phase |
|---|---|---|---|
| P0 | Real OpenAI verification (chat, streaming, embeddings, errors), with the key provided | Verification | 2 |
| P0 | Rate limiting must not fail open (F1) | Fix | 3 |
| P0 | 2FA key ring and re-encrypt command (F4) | Fix | 3 |
| P1 | Per-session access-token revocation (F2) | Fix | 3 |
| P1 | Resumable, lease-protected infrastructure migrations (F3) | Fix | 3 |
| P1 | Cross-tenant test sweep: API, search, MCP, jobs, integrations | Tests | 4 |
| P1 | Timed backup restore and recovery drill on an isolated stack | Verification | 5 |
| P2 | Orphaned-object reconciliation (F6/F7) | Fix | 3 |
| P2 | PostgreSQL statement timeout (F8) | Fix | 3 |
| P2 | Release build and clean-volume deployment check | Verification | 7 |
| Blocked | AWS S3, Azure Blob, Stripe, Slack, Google Drive, real Google sign-in, Let's Encrypt issuance | Need credentials, accounts or a public host | 2 |
