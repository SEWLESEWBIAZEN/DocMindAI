# 05 — Acceptance test results

- Date: 2026-11-27
- Code: branch `ccr-ac2d5c32-hw6qxt` at the end of Phase 5, commit `c73dd0d`.
- Environment:
  - the development stack (`docker compose`: PostgreSQL 16 + pgvector, Redis 7.4, MinIO, Mailpit,
    Azurite) for the unit, integration and browser suites;
  - the production stack (`deploy/production` smoke harness, HTTPS) for the deployment checks;
  - real external services where marked **live**.
- Every number below is from a run in this environment on this date. CI/CD is out of scope.

## 1. Results

| Suite | Command | Result |
|---|---|---|
| Backend lint and format | `ruff check src tests`, `ruff format --check` | clean; 344 files formatted |
| Backend types | `mypy src` | no issues in 251 source files |
| Backend unit | `pytest` | **489 passed** |
| Backend integration (real PostgreSQL, Redis, MinIO, Azurite) | `pytest -m integration` | **311 passed**, 1 skipped (the live test below, which needs a key in the environment) |
| Live: OpenAI adapters (chat, streaming, cancellation, embeddings, vision, errors, BYOK validation, model check) | `pytest -m live tests/live/` | **15 passed**, 1 skipped: text-to-speech, because the key can't use `gpt-4o-mini-tts`, as `ai check` also reports |
| Live: invalid credentials against real AWS S3, Stripe, Slack, Google OIDC and Drive | (same run) | included in the 15 |
| Live: the whole app against real OpenAI (upload, embeddings, hybrid search, grounded and cited answer, declined question, usage ledger) | `pytest -m "integration and live" tests/integration/test_openai_end_to_end_live.py` | **1 passed** |
| Frontend lint | `oxlint --deny-warnings` | clean |
| Frontend types | `tsc -b` | clean |
| Frontend unit | `vitest run` | **93 passed** (17 files) |
| Frontend production build | `vite build` | built; main bundle 370 kB (116 kB gzip) |
| OpenAPI contract (spec matches the app; generated client types current) | `test_openapi_contract.py` | passed (in the unit run) |
| Browser end to end, dev stack, all 17 specs | `npx playwright test` | **24 of 24 passed**: 21 in the full run, and the 3 below on re-run |
| Browser journey on the production stack over HTTPS | `smoke/run.sh test` | **5 of 5 passed** |
| TLS on the production stack | `smoke/run.sh tls` | **22 of 22** checks |
| Backup and restore drill on clean volumes | doc 04, section 5 | passed: restored in 58.6 s, every check matched |

## 2. Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| A user signs up, verifies their email, signs in and sets up 2FA | VERIFIED | `journey.spec.ts`, `mfa-email.spec.ts`, `passkeys.spec.ts`; auth and MFA integration tests |
| Upload, processing (PDF/OCR, DOCX, Markdown) and a ready document | VERIFIED | `test_processing_pipeline.py`, journey |
| Hybrid search and cited answers, grounded in the workspace's documents | VERIFIED, **with real OpenAI** | `test_openai_end_to_end_live.py` |
| A question the documents can't answer is declined, without stray citation markers | VERIFIED with real OpenAI | same test; `strip_markers` fix (doc 02) |
| Conversations, streaming, cancellation, feedback | VERIFIED | `test_conversations_api.py`, `feedback.spec.ts`, live streaming and cancellation |
| Organizations, members, roles, invitations, restricted collections | VERIFIED | `test_tenancy_api.py`, `test_collection_access_api.py`, `access.spec.ts` |
| No workspace data reachable from another tenant (API, IDs, API keys, MCP, jobs) | VERIFIED | `test_tenant_isolation_sweep.py` (doc 03, section 4) |
| API keys and MCP server | VERIFIED | `test_api_keys_and_mcp.py`, `integrations.spec.ts` |
| Billing: plans, checkout, webhooks, no duplicate subscription | VERIFIED with `FakeStripe` only | `test_billing_api.py`, `billing.spec.ts`. Real Stripe: BLOCKED (test key declined) |
| Google Drive connector, Google sign-in, Slack | VERIFIED with fakes; invalid-key path live | `connectors.spec.ts`, `google.spec.ts`, `test_slack_api.py`, live provider tests |
| Account self-service: export, email change, deletion | VERIFIED | `test_account_self_service.py`, `account.spec.ts` |
| Infrastructure: dedicated and external databases, customer buckets, migrations with crash recovery | VERIFIED | `test_infrastructure*.py`, `saas.spec.ts` |
| Accessibility: axe WCAG 2.2 A/AA on every page, light and dark; keyboard-only journey | VERIFIED (automated) | `a11y.spec.ts`. Manual screen-reader sessions are still open |
| Redis outage: limits hold, paid AI refused, metrics keep answering | VERIFIED | `test_redis_outage.py`; production-stack drill (doc 04, section 6) |
| Backups run on the production stack and restore onto empty infrastructure | VERIFIED | doc 04, sections 4–5 |
| Signing out ends that device's session at once | VERIFIED | `test_signing_out_ends_that_devices_access_token_at_once` |
| 2FA key rotation without lock-outs | VERIFIED | `test_rotating_the_mfa_key_keeps_everyone_signed_in` |

## 3. Failures seen during acceptance, and what they were

| Run | Failure | Cause | Outcome |
|---|---|---|---|
| Playwright, full run | 3 tests: the `journey.spec.ts` complete journey and mobile layout, and `integrations.spec.ts`, timed out after sign-up | The sign-up form answered **"Too many requests"**. The suite registers about 25 accounts from one IP in 13 minutes. Sign-up shares the auth limit of 10 per IP per 10 minutes, so the specs late in the run hit it | Not a product defect: the limit is working as designed. After clearing the **dev** Redis's `rl:*` counters (22 keys), the 3 tests passed. Recommendation: give the dev stack a higher `RATE_LIMIT_AUTH_PER_IP`, or reset the counters in a Playwright global setup. Never raise it in production |
| Integration, run before the Phase 4 fix | `test_transient_failures_retry_then_dead_letter_then_manual_retry` | Test timing (doc 03, P4-3) | Fixed; passed in this run |

No other failures. No test was skipped, disabled or weakened to get a green result. The two
skips above are tests that need a real key or a model the key doesn't have.

## 4. Not covered by acceptance

- **Payments:** a real Stripe test-mode checkout and webhook.
- **Real accounts** for AWS S3, Azure Blob, Slack, Google Drive and Google sign-in. Only the error
  paths were exercised against these real services.
- **Real Let's Encrypt issuance**, which needs a public DNS name.
- **Load and soak** at production volume. Phase 9 load tests exist, but weren't re-run for this
  release.
- **People:** manual screen-reader testing, and an independent penetration test.
