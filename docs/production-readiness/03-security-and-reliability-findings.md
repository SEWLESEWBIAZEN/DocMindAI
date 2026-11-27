# 03 — Security and reliability findings

- Date: 2026-11-27
- Scope: Phase 3 (reliability hardening) and Phase 4 (security and tenant isolation) of the
  production-readiness engagement. It builds on [`01-current-state-audit.md`](01-current-state-audit.md)
  (failure scenarios F1–F12) and [`02-real-integration-verification.md`](02-real-integration-verification.md).
- Method: code changes with tests against real PostgreSQL 16 + pgvector, Redis 7.4 and MinIO in
  the development stack. Where stated, it also used the real OpenAI API.
- Not covered here: CI/CD (out of scope; GitHub Actions is blocked by an account billing issue)
  and an independent penetration test (still open, see section 6).

Every result below is from a test run in this environment. Nothing is reported as verified
without a run.

## 1. Summary

| Area | Initial status | Final status | Evidence |
|---|---|---|---|
| Rate limiting when Redis is down (F1) | BROKEN: every limit switched off | VERIFIED | `test_rate_limits_hold_and_ai_is_refused_while_redis_is_down`; 5 unit tests in `test_rate_limiter.py` |
| Signed-out access tokens (F2) | BROKEN: valid up to 15 min after sign-out | VERIFIED | `test_signing_out_ends_that_devices_access_token_at_once`, `test_refreshing_keeps_the_session_and_old_tokens_still_expire_normally` |
| Crashed infrastructure migration (F3) | BROKEN: only abort, no lease | VERIFIED | 4 crash/resume tests in `test_infrastructure_migration.py` (7 of 7 pass) |
| 2FA key rotation (F4) | BROKEN: no rotation path | VERIFIED | `test_rotating_the_mfa_key_keeps_everyone_signed_in`; `TestKeyRotation` unit tests |
| Worker killed mid-job (F5) | IMPLEMENTED | VERIFIED (existing tests) | `test_sweeper_recovers_jobs_from_crashed_workers` |
| Orphaned objects (F6/F7) | IMPLEMENTED: logged only | VERIFIED | `test_orphaned_objects.py` (2 tests) |
| Slow or hung queries (F8) | BROKEN: no statement timeout | VERIFIED | `test_statement_timeout.py` (4 tests) |
| AI provider errors (F9) | to verify | VERIFIED against OpenAI | doc 02, section A; a 404 model is now `llm_misconfigured`, not retried |
| Configured model not available to the key (F10) | not detected | VERIFIED against OpenAI | `documind-manage ai check`; live test `test_configured_model_check_against_the_real_api` |
| Stripe webhooks (F11) | MOCKED | MOCKED: real test key declined | `test_billing_api.py` with `FakeStripe` |
| Startup with dependencies down (F12) | VERIFIED | VERIFIED | readiness tests |
| Cross-tenant isolation, all workspace routes | IMPLEMENTED, tested per feature | VERIFIED: sweep over every route | `test_tenant_isolation_sweep.py` (4 tests) |
| Forged processing message | not checked | VERIFIED (fixed) | `test_a_job_message_naming_another_workspace_is_refused` |
| Secrets in logs | IMPLEMENTED, partial patterns | VERIFIED (extended) | `test_redact_masks_credentials` (16 cases) |
| Dependency vulnerabilities | unknown for this release | VERIFIED: none known | section 4 |
| Flaky processing test | BROKEN (1 in ~2 full runs) | FIXED at cause | 3 of 3 isolated runs; the next full-suite run is in Phase 6 |

Full-suite runs after these changes:
- unit tests: 478 passed;
- integration tests: 305 passed, 1 skipped (the live OpenAI end-to-end test, which needs a key in
  the environment), and 1 failed. The failure was the flaky test in section 3, fixed afterwards.

## 2. Reliability fixes (Phase 3)

### F1 — Rate limiting no longer fails open

**Before:** `RedisRateLimiter.hit` caught `RedisError` and let the request through. During a Redis
outage there was no limit on sign-in, password reset, 2FA codes or AI requests.

**Now** (`infrastructure/ratelimit/redis_limiter.py`):
- **Most scopes** fall back to a per-process in-memory counter, bounded to 50,000 keys. With N API
  processes, an attacker gets at most N times the limit, and Argon2 still keeps each guess slow.
- **AI answers and speech** (`ai:user`, `voice:user`) are refused with 503
  `rate_limiter_unavailable`. Per-process counters would multiply a paid allowance.
- **Retries:** Redis is retried at most every 5 s, so a hung Redis doesn't add its timeout to every
  request.
- **Monitoring:** entering and leaving degraded mode are each logged once. The
  `documind_rate_limiter_degraded` metric drives the `DocuMindRateLimiterDegraded` alert.

**Test:** the integration test stops Redis in the middle of a run. Sign-in limits still apply, AI
requests get 503, and everything recovers when Redis returns.

### F2 — Signing out ends that device's access token at once

**Before:** single-device sign-out revoked the refresh-token family. The access token kept working
for up to 15 minutes.

**Now:**
- Access tokens carry a `sid` claim (the refresh family). `authenticate()` checks that the family
  is still active.
- Step-up tokens inherit `sid`.
- Tokens issued before this change have no `sid` and keep working until they expire (15 min), so
  the deployment signs nobody out.

### F3 — Crashed migrations resume

**Before:** a dead run left the workspace locked until an operator ran `abort`, which threw the
copy away. Nothing stopped two runners from working on the same migration.

**Now** (`application/infrastructure/migration.py`):
- **Lease:** a run holds a lease, made of `plan.runner` and `plan.heartbeat_at`. The heartbeat is
  refreshed between tables and every 25 files.
- **Recorded steps:** `preflight_passed`, `rows_copied`, `objects_copied` and `verified`.
- **Resume:** `documind-manage migrations resume <id>` takes over once the heartbeat is more than
  5 minutes old. It refuses with `migration_active` while the heartbeat is fresh.
- **On resume:**
  - An unfinished row copy committed nothing, because it is one transaction, so it is redone.
  - Files already at the destination with the right size are skipped.
  - The copy is verified again before the switch.
  - If the process died after the switch, resume only marks the migration completed.
- **Lost lease:** a runner whose lease was taken stops without clean-up, so it can't delete the
  copy that the new runner is using.

**Tests** (all against a real external PostgreSQL and a MinIO customer bucket):
- a crash after the file copy;
- a crash in the middle of the row copy;
- a crash after the switch;
- a refusal while the run is still alive;
- a refusal when the migration isn't running;
- `LeaseLostError` for the old runner.

### F4 — 2FA key rotation without lock-outs

**Before:** one `MFA_ENCRYPTION_KEY`, with no key id in the ciphertext. Changing the key locked
out every 2FA user.

**Now:**
- `MFA_ENCRYPTION_KEYS_PREVIOUS` holds the old keys. New ciphertexts are written as
  `v2:<key id>:<payload>`, and legacy `v1` ciphertexts decrypt by trying each key.
- Recovery-code and email-code hashes are checked against every pepper.
- `documind-manage mfa rewrap [--dry-run]` re-encrypts stored secrets in batches. It never prints a
  secret.
- If a stored secret can't be decrypted, the user gets 503 `mfa_unavailable` instead of a 500.
- The procedure is in [`runbooks/disaster-recovery.md`](../runbooks/disaster-recovery.md).

### F6/F7 — Orphaned objects

`documind-manage orphans [--delete] [--min-age-hours 24]` (`ops/orphans.py`) lists objects in the
platform bucket that no row refers to. By default it only reports.

**Never touched:**
- objects newer than the minimum age;
- workspaces being migrated;
- the retained source copy of a workspace whose storage moved to its own bucket;
- unknown prefixes;
- any customer bucket.

**Tests:**
- An object left by a failed delete, one from a deleted workspace, and an export whose row is gone
  are all found and deleted.
- The live document and an unknown object are kept.
- The retained source after a real migration is skipped.

### F8 — Statement timeout

- **The setting:** `DATABASE_STATEMENT_TIMEOUT_SECONDS`, default 30, applies to API connections on
  the control plane and on data planes. A cancelled statement answers 503 `database_timeout` with
  `Retry-After`. Other database errors stay 500s.
- **Exempt:** workers and the CLI have no timeout, because copies, backups and exports run long.
- **Connection names:** `documind-api`, `documind-worker` and `documind-cli`, visible in
  `pg_stat_activity`.

### F10 — Configured-model check

- **When it runs:** once in the background at API start, so it never delays readiness.
  `documind-manage ai check` runs the same check, as does `eval`.
- **How:** it asks each provider about each configured model with `GET /v1/models/{model}`, which
  costs nothing.
- **Outputs:** a log line, the metric `documind_ai_model_available{purpose,model}`, and the
  `DocuMindAIModelUnavailable` alert.
- **Result against the real API with the verification key:**
  - `text-embedding-3-small` and the allowed chat model: available.
  - The default `LLM_MODEL=gpt-5.4-mini` and both default speech models: **missing**.
  - Production must set `LLM_MODEL` to a model the key can use.
- **Not verified here:** a rejected key. The egress proxy substitutes the saved key, so the
  rejected-key path is covered by unit tests only.

## 3. Defects found and fixed in Phase 4

| # | Severity | Finding | Fix | Evidence |
|---|---|---|---|---|
| P4-1 | Low (defense in depth) | A processing message's workspace chose the data plane and storage, but nothing checked it matched the job's own workspace. A forged message (which needs access to the internal Redis broker) could pair one tenant's job with another tenant's bucket. | The orchestrator refuses a message naming another workspace, **before** claiming the job, so the genuine message still works | `test_a_job_message_naming_another_workspace_is_refused` |
| P4-2 | Medium | Log redaction missed several secret formats the app handles: workspace API keys (`dmk_`), SCIM tokens (`dmscim_`), Stripe secret and restricted keys (`sk_live_`, `rk_test_`…), Stripe webhook secrets (`whsec_`), Slack tokens (`xoxb-`…), and `token=`, `refresh_token=` and `client_secret` assignments | Patterns added. Ordinary fields such as `input_tokens=812` are left alone | `test_redact_masks_credentials`, `test_redact_keeps_ordinary_text` |
| P4-3 | Low (test) | `test_transient_failures_retry_then_dead_letter_then_manual_retry` failed in about one of two full-suite runs. **Cause:** the suite's 1 s retry base could expire before the second `run_job` finished building its container, so the job was due again. Not a product defect: production uses a 30 s base with ±20 % jitter. | The test uses a 120 s base and moves the retry time itself, as it already did | 3 of 3 isolated runs and the sweep run |

**Checked, no defect:**
- **Request logs:** the request-logging middleware records the path only, never the query string,
  so OAuth `code` and `state` values don't reach the logs. Uvicorn's access log is off.
- **Account exports:** another user gets 404 for someone else's export download
  (`test_account_self_service.py`).

## 4. Tenant isolation sweep

`tests/integration/test_tenant_isolation_sweep.py` reads the operations from the running app's
OpenAPI document, so a route added later is covered without editing the test.

**Victim setup:** the victim is an Enterprise organization with real content, each with its real
ID: a processed document, a conversation and message, a collection, an invitation, an API key, a
member, a deadline, a customer worker and an infrastructure version. The attacker owns another
organization and uses a fresh 2FA check, so step-up requirements can't mask a leak.

| Attack | Operations | Result |
|---|---|---|
| Outsider calls every workspace operation on the victim's workspace | 111 | 111 × 404: the attacker can't tell whether the workspace exists |
| Victim's resource IDs inside the attacker's own workspace (IDOR) | 41 (every operation taking an ID the victim really has) | 41 × 403/404. Valid bodies are sent, so the ID lookup is reached; the test fails on a 422 |
| The attacker's workspace API key (all scopes) on every victim route, plus the victim's MCP endpoint | 111 + MCP `tools/call search` | no 2xx |
| Forged processing message (victim's job, attacker's workspace) | 1 | refused (`skipped`); the genuine message then succeeds |

**Integrity check:** after each attack, the victim's documents, members, collections and their
access settings, messages, API keys, deadlines and infrastructure are compared with a snapshot
taken before it. All were unchanged.

**Sanity check:** a temporary diagnostic confirmed the sweep isn't passing on wrong URLs. The same
URL construction against the attacker's own workspace returned 200 for all 24 parameterless GETs.

**Feature-level isolation tests that still apply:**
- `test_tenancy_api.py`: membership and roles;
- `test_collection_access_api.py`: restricted collections over the API, search, chat and MCP;
- `test_rag_api.py` and `test_processing_pipeline.py::test_processing_data_is_isolated_between_workspaces`:
  retrieval filters;
- `test_infrastructure.py::test_dedicated_databases_isolate_organizations`: separate databases;
- `test_api_keys_and_mcp.py`: key scopes and routes.

**Not covered by the sweep:**
- routes under `/me` (per user, covered by their own tests);
- `extraction run_id`, `connection_id` and `migration_id`, which are filled with random IDs because
  creating real ones needs a model call, a Google OAuth client or an external database. The
  migration and connector tests check these within their own features.

## 5. Security controls reviewed

| Control | Status | Evidence |
|---|---|---|
| Workspace context resolved from the database on every request | VERIFIED | sweep above |
| CSRF header on cookie-authenticated refresh and logout | VERIFIED | `test_auth_api.py` |
| Argon2id passwords; rotating refresh tokens; reuse detection revokes the session | VERIFIED | `test_reused_refresh_token_revokes_the_session` |
| Sign out everywhere invalidates access tokens | VERIFIED | `test_logout_everywhere_invalidates_access_tokens_immediately` |
| 2FA step-up for destructive and administrative actions | VERIFIED | `test_archive_and_mfa_api.py`, `test_api_keys_and_mcp.py`, `test_infrastructure.py` |
| SSRF policy and re-check before connecting (customer hosts) | VERIFIED | `test_infrastructure_profiles.py`, `test_customer_hosts_are_rechecked_before_connecting` |
| Webhook signatures | Slack: VERIFIED (real HMAC algorithm, fake Slack). Stripe: IMPLEMENTED with the Stripe SDK's `Webhook.construct_event`; tests use `FakeStripe`, so the real check hasn't run here | `test_slack_api.py`, `test_billing_api.py` |
| API security headers; validation errors don't echo input | VERIFIED | `test_security_hardening.py` |
| TLS 1.2/1.3, HSTS, redirect, renewal reload | VERIFIED on the production stack | doc 02, section TLS (22/22 checks) |
| Secrets never in logs | VERIFIED (extended, P4-2) | `test_security_hardening.py` |
| Rate limits hold during a Redis outage | VERIFIED (F1) | section 2 |

**Dependency audit (this release):**

| Tool | Scope | Result |
|---|---|---|
| `pip-audit` (PyPI advisory database) | 257 locked runtime packages (`uv export --no-dev`) | No known vulnerabilities |
| `pip-audit` (PyPI advisory database) | 284 locked packages, including dev | No known vulnerabilities |
| `pip-audit -s osv` | runtime packages | **BLOCKED**: `api.osv.dev` is not on this environment's network allow-list |
| `npm audit` | frontend, production and dev | 0 vulnerabilities |

## 6. Known limitations and open items

- **Stripe** is verified only with fakes and an invalid-key request against the real API. The test
  key was declined in this engagement. A test-mode checkout and webhook run is needed before taking
  live payments.
- **AWS S3, Azure Blob, Slack, Google Drive and Google sign-in** are verified only on their
  error paths against the real services (doc 02). The happy paths need accounts.
- **Penetration test:** an independent one, including tenant breakout through dedicated and
  external data planes, is still open.
- **Orphans and resume are operator steps.** `orphans` is not scheduled, and a dead migration is
  not resumed automatically. Both are deliberate: each needs a human to read the report first.
- **The rate-limit fallback is per process.** During a Redis outage, limits are N times looser with
  N API processes. Paid scopes are refused instead.
- **The DNS rebinding window** for customer hosts remains, as accepted in
  [`SECURITY_REVIEW.md`](../SECURITY_REVIEW.md).
- **Release images were not rebuilt** from the Dockerfiles in this environment (Docker Hub rate
  limits). See Phase 7.
