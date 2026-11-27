# 06 — Release readiness

- Date: 2026-11-27
- Code: branch `ccr-ac2d5c32-hw6qxt`.
- Earlier reports:
  - [01 audit](01-current-state-audit.md)
  - [02 real integrations](02-real-integration-verification.md)
  - [03 security and reliability](03-security-and-reliability-findings.md)
  - [04 operations](04-operational-verification.md)
  - [05 acceptance](05-acceptance-test-results.md)
- Statuses:
  - **IMPLEMENTED:** the code exists.
  - **MOCKED:** exercised only against a stand-in.
  - **CONFIGURED:** set up but untested.
  - **VERIFIED:** tested for real in this engagement.
  - **BLOCKED:** needs credentials, accounts or access that weren't available.
  - **BROKEN:** a defect is open.

## 1. Decision

**Ready for a controlled launch** (invited customers, no card payments) once the three
"before launch" items in section 4 are done.

**Not yet ready for general availability with paid plans:** Stripe has never run against a real
test-mode account, and the release images haven't been built from their Dockerfiles. Both need
people or access outside this environment (section 4).

**No open BROKEN items.** Every defect found in this engagement is fixed and covered by a test
(section 3).

## 2. Final summary

| Area | Initial status | Final status | Evidence |
|---|---|---|---|
| **OpenAI:** chat, streaming, cancellation, embeddings, vision, timeouts, BYOK validation | IMPLEMENTED, MOCKED | VERIFIED (live) | doc 02 §A; 15 live tests; live end-to-end test |
| **OpenAI speech** (STT/TTS) | IMPLEMENTED, MOCKED | BLOCKED | the key can't use the speech models (`ai check`: missing) |
| **Anthropic** (chat) | IMPLEMENTED, MOCKED | BLOCKED | no key |
| **RAG end to end:** hybrid search, grounded and cited answers, declining unanswerable questions | IMPLEMENTED | VERIFIED (live OpenAI) | `test_openai_end_to_end_live.py` |
| **AWS S3, Stripe, Slack, Google OIDC and Drive** | IMPLEMENTED, MOCKED | VERIFIED on the error path only (real services, invalid credentials); happy path BLOCKED | doc 02; `tests/live/test_providers_live.py` |
| **Azure Blob** | IMPLEMENTED, MOCKED (Azurite) | MOCKED (Azurite); real account BLOCKED | Azurite integration tests |
| **MinIO / S3 API storage** | VERIFIED (dev) | VERIFIED (production stack) | doc 04 |
| **MCP server** | VERIFIED (local) | VERIFIED, including cross-tenant | `test_api_keys_and_mcp.py`, sweep |
| **Stripe billing logic** | IMPLEMENTED, MOCKED | MOCKED (`FakeStripe`); real test mode BLOCKED | `test_billing_api.py` |
| **TLS** (Nginx, HSTS, redirect, renewal reload) | CONFIGURED | VERIFIED (production stack); real Let's Encrypt BLOCKED | 22/22 `tls-check.sh`, twice, including a clean deployment |
| **Rate limiting when Redis is down** (F1) | BROKEN | VERIFIED | doc 03 §2; production drill (doc 04 §6) |
| **Single-device sign-out** (F2) | BROKEN | VERIFIED | doc 03 §2 |
| **Crashed migration recovery** (F3) | BROKEN | VERIFIED | 4 crash/resume tests |
| **2FA key rotation** (F4) | BROKEN | VERIFIED | rotation integration test |
| **Worker crash mid-job** (F5) | IMPLEMENTED | VERIFIED | `test_sweeper_recovers_jobs_from_crashed_workers` |
| **Orphaned objects** (F6/F7) | IMPLEMENTED (logged only) | VERIFIED | `documind-manage orphans`; 2 tests |
| **Statement timeout** (F8) | BROKEN | VERIFIED | 4 tests |
| **AI provider errors and timeouts** (F9) | to verify | VERIFIED (live) | doc 02 |
| **Configured-model check** (F10) | not detected | VERIFIED (live) | start-up check, metric, alert, `ai check` |
| **Webhook ordering and duplicates** (F11) | MOCKED | MOCKED | fakes; needs Stripe test mode |
| **Startup with a dependency down** (F12) | VERIFIED | VERIFIED | readiness tests; production drill |
| **Tenant isolation, all workspace routes** | IMPLEMENTED, per feature | VERIFIED | 111 + 41 + API-key/MCP sweep (doc 03 §4) |
| **Secrets in logs** | IMPLEMENTED, partial | VERIFIED | extended redaction (doc 03, P4-2) |
| **Dependency vulnerabilities** | unknown | VERIFIED: none known | pip-audit (284 packages), npm audit |
| **Health, readiness, metrics** | CONFIGURED | VERIFIED (production stack) | doc 04 §2–3 |
| **Metrics during a Redis outage** | BROKEN (found in Phase 5) | VERIFIED | doc 04 §6, D5-1/D5-2 |
| **Scheduled backups** | BROKEN (found in Phase 5) | VERIFIED | doc 04 §4, D5-3/D5-4 |
| **Restore onto clean infrastructure** | VERIFIED (integration test) | VERIFIED (timed drill: 58.6 s to healthy) | doc 04 §5 |
| **Offsite backup copy** | CONFIGURED | CONFIGURED | no offsite bucket here |
| **Alert rules in Prometheus** | CONFIGURED | CONFIGURED (syntax and runbook links tested) | `test_alert_runbooks.py` |
| **Clean-volume deployment** | not done | VERIFIED | §5 below |
| **Release images built from the Dockerfiles** | not done | BLOCKED | Docker Hub 429; the registry-mirror change was not permitted here |
| **Automated accessibility** | VERIFIED | VERIFIED | `a11y.spec.ts` (light and dark, keyboard) |
| **CI/CD** | out of scope | out of scope | GitHub Actions blocked by account billing |

## 3. Defects fixed in this engagement

| Phase | Defect | Severity |
|---|---|---|
| 2 | OpenAI 404 (unknown model) treated as transient and retried | Medium |
| 2 | `[n]` citation markers left in declined answers | Low |
| 2 | `httpx` imported at runtime but installed only as a dev dependency | High: crash in production images |
| 2 | Expired smoke certificates; Nginx default site broke the HTTP redirect | Low (smoke harness) |
| 3 | F1, F2, F3, F4, F6/F7, F8, F10 (section 2) | High to low |
| 4 | Processing message could name another workspace (P4-1) | Low (defense in depth) |
| 4 | Log redaction missed API keys, SCIM, Stripe, webhook and Slack secrets (P4-2) | Medium |
| 4 | Flaky retry test (P4-3) | Test only |
| 5 | `/metrics` answered 500 while Redis was down (D5-1) | Medium: blind monitoring during an outage |
| 5 | Rate-limiter outage signal never cleared in quiet processes (D5-2) | Low: a false alert |
| 5 | Backups needed `PGURL` as a second copy of the database URL (D5-3) | High: nightly backups fail |
| 5 | Backup image had no writable `/backups` (D5-4) | High: **every** backup failed |
| 5 | Smoke OpenAI stub lacked `GET /v1/models/{id}` | Test harness |

## 4. Before launch

### Before any customer

1. **Set `LLM_MODEL`** to a chat model the production key can use. With the key used here, the
   default `gpt-5.4-mini` is not available. Either set `SPEECH_PROVIDER=none` or allow the speech
   models for the key. Confirm with `documind-manage ai check`, which must exit 0.
2. **Build and scan the release images** (`backend` runtime, worker, backup; `frontend`) from the
   Dockerfiles, in CI or on a machine with Docker Hub access. Run `smoke/run.sh all` against those
   images, without `SMOKE_USE_SOURCE`. This is the first build of the `backup` stage since the D5-4
   fix.
3. **Deploy on the real host:**
   - issue the real Let's Encrypt certificate;
   - configure the offsite backup bucket;
   - take one backup (`BACKUP_ONCE=true`) and confirm the offsite copy arrives;
   - point Prometheus at `alerts.yml` and send one test alert through to the on-call channel.

### Before taking payments

4. **Stripe test mode, end to end:** checkout, webhook delivery (including duplicates and
   out-of-order delivery), the customer portal, cancellation, and a failed payment. This needs a
   test-mode key and webhook secret, kept separate from live.

### Before general availability

5. An independent **penetration test**, including tenant breakout through dedicated and external
   data planes.
6. **Real-account checks** for each connector you sell: Google Drive, Slack, Google sign-in,
   customer S3 and Azure buckets.
7. A **restore drill on production-sized data**, to set the customer-facing RTO. Decide the RPO;
   under 24 h needs PostgreSQL WAL archiving.
8. **Manual screen-reader testing** (NVDA, JAWS, VoiceOver).

## 5. Clean-volume deployment

The production smoke stack was destroyed with `down -v`. Its database, Redis and object volumes
were removed; only the `backups` volume of the separate `backup` profile remained. It was then
deployed from nothing.

| Step | Result |
|---|---|
| `smoke/run.sh up` | healthy in 54 s; 23 control-plane migrations applied from scratch (`0023`); 0 users |
| `smoke/run.sh test` (Playwright over HTTPS) | 5 of 5 passed |
| `smoke/run.sh tls` | 22 of 22 |
| `documind-manage ai check` | ok (stub provider) |

## 6. Known limitations

**Not verified here:**
- **Images** ran as the current source over the last built image with the same locked
  dependencies, not as freshly built images. The backup image came from a drill-only Dockerfile.
- **Stripe, Slack, Google Drive, Google sign-in, AWS S3 and Azure:** only the error paths were
  exercised against these real services.
- **No Prometheus server, offsite bucket or public DNS** in this environment.
- **No load test** at production volume for this release.

**By design:**
- **Rate-limit fallback:** during a Redis outage the limits are per API process, so N processes
  give N times the limit. Paid AI requests are refused instead.
- **Operator steps:** `orphans` and `migrations resume` are run by an operator, deliberately not
  automatically.
- **RPO is 24 h** with the default schedule.
- **Full e2e suite and the sign-up limit:** the full browser suite exceeds the production sign-up
  rate limit from one IP (doc 05 §3).

## 7. Recommendations

1. Run `documind-manage ai check` as a deployment gate (exit 1 blocks the rollout). Keep
   `DocuMindAIModelUnavailable` paging.
2. Restore CI and add three jobs:
   - the integration suite;
   - the tenant sweep (`test_tenant_isolation_sweep.py`), which covers new routes automatically;
   - `smoke/run.sh all` on built images.
3. ~~Give the dev stack its own sign-up limit.~~ Done: the development `docker-compose.yml` sets
   `RATE_LIMIT_AUTH_PER_IP` and `RATE_LIMIT_LOGIN_PER_IP` to 200, overridable in `.env`. The
   production stack keeps the defaults; never change those.
4. Schedule `documind-manage orphans` monthly in report-only mode, and review before `--delete`.
5. Add PostgreSQL WAL archiving if customers need an RPO under a day.
6. Add an OSV-based dependency scan to CI. Only the PyPI advisory database was reachable here.
