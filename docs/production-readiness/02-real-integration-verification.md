# 02 — Real integration verification

- Date: 2026-11-27
- Environment: a cloud engineering container, with outbound HTTPS through an egress proxy
  that has an allow-list. The DocuMind code is commit `178e481` plus the fixes listed below.
- Credentials: an OpenAI API key was provided through the platform's secret store; the proxy
  injects it, so it never appears in this repository, the logs or this report. A Stripe test
  key was requested and declined. No AWS, Azure, Google OAuth client or Slack app credentials
  were available.

Results:
- **VERIFIED:** passed against the real service.
- **VERIFIED (negative path):** the real service was reached with deliberately invalid
  credentials, and DocuMind handled its real error correctly. This proves connectivity, the
  request format and error handling, **not** the happy path.
- **BLOCKED:** needs something not available here (listed under "To unblock").
- **FIXED:** a defect was found against the real service and fixed.

Reproduce:
- Live checks are opt-in pytest markers, so normal test runs never call paid APIs:
  ```
  # Unit-level adapters against real OpenAI:
  OPENAI_API_KEY=… LIVE_OPENAI_CHAT_MODEL=<chat model the key can use> uv run pytest -m live tests/live/test_openai_live.py -v -s
  # Full app path (needs the dev stack: PostgreSQL, Redis, MinIO):
  OPENAI_API_KEY=… LIVE_OPENAI_CHAT_MODEL=… uv run pytest -m "integration and live" tests/integration/test_openai_end_to_end_live.py -v -s
  # AWS S3, Stripe, Slack, Google with invalid credentials (no account needed):
  uv run pytest -m live tests/live/test_providers_live.py -v -s
  ```
- The full logs are kept outside the repository because they contain account identifiers.
  Excerpts below are redacted.

## A. OpenAI

The key's project can use exactly two models: `text-embedding-3-small` and one chat model
(`gpt-6-luna`). The tests ran with `LIVE_OPENAI_CHAT_MODEL` set to that chat model.

| # | Scenario | Expected | Actual | Result |
|---|---|---|---|---|
| A1 | Authentication: list models | 200, models listed | 200; 2 models | VERIFIED |
| A2 | BYOK validation (`ProviderAdapterFactory.validate`) with usable models | `ok=True` | `ok=True`, both models checked | VERIFIED |
| A3 | BYOK validation with an unusable model | `model_not_available`, no provider text shown | `model_not_available`, user-safe message | VERIFIED |
| A4 | Chat completion with usage | Text and token counts | `pong`, 13 in / 15 out, `stop` | VERIFIED |
| A5 | Streaming | Deltas, then usage from the final chunk | Deltas, then usage (13/4), `stop` | VERIFIED |
| A6 | Cancel a stream mid-answer (browser disconnect) | Generator closes without error | Closed cleanly after 2 deltas | VERIFIED |
| A7 | Model not available to the key, or doesn't exist | A configuration error, not a transient glitch | **Was:** 404 mapped to `llm_error` "Unexpected AI service response (404)", retried as transient. **Now:** `llm_misconfigured` | **FIXED**, re-verified |
| A8 | The app's default model `gpt-5.4-mini` with this key | Configuration error | 403 "project does not have access to model", mapped to `llm_misconfigured` | VERIFIED. Operators must set `LLM_MODEL` to a model their key can use |
| A9 | Timeout (10 ms limit, no retries) | Bounded, classified | `llm_unavailable` after 0.07 s | VERIFIED |
| A10 | Embeddings | 1536-dim vectors (the pgvector column), token count | 2 × 1536, 6 tokens | VERIFIED |
| A11 | Vision: describe a generated bar chart | A faithful description | "Bar chart titled 'Sales 2024'… three blue bars… increasing" (175/83 tokens) | VERIFIED (with the chat model) |
| A12 | Speech (TTS `gpt-4o-mini-tts`, STT `gpt-4o-mini-transcribe`) | Round trip | 403 for the TTS model, classified `llm_misconfigured` | BLOCKED: the key has no speech models. The error handling is verified |
| A13 | Rate limit (429) | `llm_rate_limited`; the SDK retries a bounded number of times | Not triggered deliberately (cost and abuse); covered by unit tests with a 429 response | BLOCKED (deliberately not triggered) |
| A14 | Invalid API key against the real API | `llm_misconfigured` | Not testable here: the egress proxy replaces the Authorization header with the saved key. Covered by unit tests (401) | BLOCKED (environment) |
| A15 | API keys never in logs or responses | — | The adapters log only "LLM provider rejected our credentials", never the provider's message (which can contain the project id). The live logs contain no key | VERIFIED by inspection |
| A16 | **End to end through the app:** upload → real embeddings → hybrid search → streamed answer → follow-up → unanswerable question → usage ledger | Grounded answer with a verified citation; decline when the answer isn't there; usage recorded | 2 chunks embedded; hybrid search found the passage; a streamed, grounded answer cited "policy, section Approvals"; a question the policy doesn't cover was declined (`insufficient_context`); the ledger recorded 3 answers and 1,266 + 232 chat tokens and 26 query-embedding tokens | VERIFIED |
| A17 | Answer correctness: a "not in the documents" answer that still contained `[1]` | No citation marker without a citation | **Was:** `[1]` left in the text with no citation card (seen with the real model). **Now:** markers are stripped from `insufficient_context` answers | **FIXED** (unit-tested; the next live run uses it) |

Cost: about 3,000 tokens in total across all runs, well under US$0.05 at small-model prices.

## B. AWS S3

| Scenario | Actual | Result |
|---|---|---|
| Storage validation (`validate_storage`) against real AWS with an unknown access key | AWS answered `InvalidAccessKeyId`; DocuMind reported `storage_rejected` "The access key isn't valid." with no secret in the result | VERIFIED (negative path) |
| Upload, download, metadata, delete, presigned URLs, permission denied, tenant prefixes, archive/delete consistency | Same boto3 adapter, tested end to end against MinIO (S3-compatible), including customer buckets with prefixes and signed downloads | BLOCKED on real AWS (no account) |

To unblock:
1. Create a test bucket (private, Block Public Access on).
2. Create an IAM user with `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` and
   `s3:ListBucket` on that bucket only.
3. In DocuMind: Infrastructure → Storage → AWS S3, bucket, region, access key. Validation runs
   the write/read/delete probe.
4. Upload, ask, download and delete a document, and check the object prefix in the bucket.

## C. Azure Blob Storage

| Scenario | Result |
|---|---|
| All | BLOCKED. `<account>.blob.core.windows.net` isn't on the egress allow-list (the account name is unknown) and there's no storage account. The adapter passes 3 tests against Azurite, including read-only SAS downloads and refusing public containers |

To unblock:
1. Create a storage account and a private container.
2. Allow `<account>.blob.core.windows.net`.
3. In DocuMind: Infrastructure → Storage → Azure Blob, with the account key. Validation runs
   the same probe.

## D. Let's Encrypt / TLS

The real production configuration was tested:
- `deploy/production/docker-compose.yml`;
- `nginx/documind.conf.template` and `nginx/entrypoint.sh`;
- the `certbot.sh` renewal loop;
- the production environment, including the production config validation.

It ran on this host through the smoke harness (`deploy/production/smoke/run.sh up`), with a
local CA standing in for Let's Encrypt. Reproduce:
```
deploy/production/smoke/run.sh up && deploy/production/smoke/run.sh tls && deploy/production/smoke/run.sh test
```

| # | Scenario | Actual | Result |
|---|---|---|---|
| D1 | ACME staging directory reachable | HTTP 200 | VERIFIED |
| D2 | Chain verifies; certificate covers the host and refuses other names | Verify return code 0 for `localhost`; failure for `evil.example` | VERIFIED |
| D3 | TLS 1.3 and 1.2 accepted; TLS 1.1 and 1.0 refused | 1.3 (`TLS_AES_256_GCM_SHA384`) and 1.2 accepted; 1.1 and 1.0 rejected **by the server** with alert 70 "protocol version", even when the client allowed them | VERIFIED |
| D4 | No weak ciphers; forward secrecy | `AES128-SHA` (CBC/SHA1) refused; TLS 1.2 negotiates ECDHE | VERIFIED |
| D5 | HTTP → HTTPS redirect, while the ACME path stays on HTTP | `301 https://localhost/login`; `/.well-known/acme-challenge/*` served without a redirect | VERIFIED, after the harness fix below |
| D6 | Security headers | HSTS (2 years, subdomains), CSP `default-src 'self'`, nosniff, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, COOP, Permissions-Policy; no nginx version disclosed | VERIFIED |
| D7 | `/api/v1/metrics` not public | 404 through nginx | VERIFIED |
| D8 | Renewal picked up without downtime | A new certificate (90 days) was written to the certificate volume and `nginx -s reload` run (what the 6-hourly loop does): the new serial was served, with **0 of 40** requests failing during the reload | VERIFIED |
| D9 | Renewal failure is visible before expiry | **Was:** only a log line in the certbot container ("renewal attempt failed"); no metric or alert. **Now:** `/metrics` exports `documind_tls_certificate_expiry_timestamp` and `documind_tls_certificate_self_signed`, with four alerts (under 14 days, under 3 days, still self-signed, unreadable) and a runbook section | **FIXED**. Live value on the stack: `1803520178` (25 Feb 2027 01:49:38 UTC), matching the served certificate |
| D10 | The application over HTTPS in production mode | Playwright journey and SaaS specs: **5/5 passed** (sign-up, upload, cited answer, team, 2FA, mobile layout) | VERIFIED |
| D11 | Real Let's Encrypt issuance and the HTTP-01 challenge | Needs a public DNS name pointing at a server | BLOCKED |

**Defects found by this run:**
- **Release blocker: `httpx` was a dev-only dependency.** The OIDC sign-in, Slack and Google
  Drive clients import it, so a clean production install crashed at startup in
  `build_container` (`ModuleNotFoundError: httpx`). It's now a runtime dependency, and
  `tests/unit/test_packaging.py` fails if any import isn't in the locked runtime dependency
  set. The test was confirmed to fail on the old lock and pass on the new one.
- **Smoke harness reused expired certificates.** Its CA and certificates last 2 days and were
  only created once, so later runs failed verification. They're now re-issued when they
  expire within a day.
- **Stock nginx default site.** The smoke harness runs the stock `nginx` image, whose default
  `server_name localhost` site answered plain HTTP instead of redirecting. The runtime image
  already deleted it at build time; the entrypoint now deletes it too, so the configuration
  is self-contained.

**Environment notes:**
- Docker Hub rate-limited base-image pulls (HTTP 429). The application image was therefore the
  previous `documind-backend:runtime` with the current locked dependencies installed by uv from
  `uv.lock`, and the current source mounted (`SMOKE_USE_SOURCE=1`). The dependency set is the
  same as a clean build, but the image itself wasn't rebuilt from the Dockerfile. Rebuild
  from scratch before a release (`06-release-readiness.md`).
- Nginx ran on the stock `nginx:1.27-alpine` image with the repository's template and
  entrypoint, as the production web image does.

To unblock D11: on a server with DNS, set `LETSENCRYPT_STAGING=true`, start with
`--profile letsencrypt`, then run `smoke/tls-check.sh <host> 443 80 <staging root CA>`. Then
switch to production Let's Encrypt and check with the system CA bundle.

## E. Google sign-in, Google Workspace SSO, SCIM

| Scenario | Actual | Result |
|---|---|---|
| OIDC discovery and keys from `accounts.google.com` | Real discovery document parsed; token endpoint `oauth2.googleapis.com` | VERIFIED |
| Code exchange with an invalid client | Google answered 401; DocuMind raised `OidcError`, so the user sees "Google couldn't confirm who you are" | VERIFIED (negative path) |
| Full sign-in, `hd` claim, nonce and state with a real Google account | Covered by tests with a fake provider that signs real RS256 tokens | BLOCKED: needs an OAuth client and a browser sign-in |
| SCIM provisioning and deprovisioning | Protocol tests | BLOCKED. **Note:** Google Workspace only provisions to apps in its catalog, so SCIM for a custom app is verified with Okta or Microsoft Entra ID, not Google |

To unblock: follow the "Continue with Google" steps in `docs/CONFIGURATION.md`. Then sign in
with a Workspace account (organization SSO) and a Gmail account (sign-in for everyone).

## F. Google Drive

| Scenario | Actual | Result |
|---|---|---|
| Refresh-token exchange with an invalid client | Google `invalid_client`, raised as `ConnectorAuthError`, so the connection shows "reconnect" | VERIFIED (negative path) |
| Drive `files.list` with an invalid access token | 401, raised as `ConnectorAuthError` | VERIFIED (negative path) |
| Authorization, first and incremental sync, updates, deletes, duplicates, revoked access | Tested against a fake Drive API | BLOCKED: needs an OAuth client with the Drive scope and a test Google account |

## G. Stripe (test mode)

| Scenario | Actual | Result |
|---|---|---|
| API call with an invalid key | Stripe 401, raised as `BillingGatewayError("AuthenticationError")` with the key not in the error | VERIFIED (negative path) |
| Checkout, webhooks (signature on the raw body, duplicates, out of order), subscription lifecycle, failed payment, portal | `FakeStripe` tests, including duplicate-subscription prevention. The owner reported a working test-mode checkout and webhook on their machine; it's not recorded here | BLOCKED: the test-mode key was declined in this session |

To unblock, in test mode only:
```
stripe listen --events checkout.session.completed,customer.subscription.created,customer.subscription.updated,customer.subscription.deleted,invoice.paid,invoice.payment_failed --forward-to http://127.0.0.1:8000/api/v1/billing/webhook
stripe trigger checkout.session.completed
stripe events resend <evt_id>                      # duplicate delivery
```
Then check the plan on the Billing page and in `billing_events`. Live merchant processing
depends on an eligible business and Stripe account; it's out of scope.

## H. MCP

| Scenario | Result |
|---|---|
| Initialize, tool discovery, tool calls, scopes, restricted collections, Origin check, protocol-version header, invalid key (401), revoked key | VERIFIED locally: `test_mcp_server_tools`, `test_keys_are_scoped_to_their_workspace_routes_and_scopes`, plus the official MCP Python SDK client against the local server (ADR 0037) |

## I. Slack

| Scenario | Actual | Result |
|---|---|---|
| `oauth.v2.access` with invalid app credentials and code | Slack `invalid_code`, raised as `SlackError` | VERIFIED (negative path) |
| `chat.postMessage` with an invalid bot token | Slack `invalid_auth`, raised as `SlackError` | VERIFIED (negative path) |
| Install, signed requests, events, retries, linking, private and channel answers | Fake Slack Web API, with real HMAC signing | BLOCKED: needs a Slack app in a development workspace |

## Fixes made in this phase

See also D (TLS): the `httpx` runtime dependency, the TLS expiry metrics and alerts, and two
smoke-harness fixes.

1. **Model not available (404) was treated as a transient error.** `raise_classified` and the
   embedding adapter now map `openai.NotFoundError` to `llm_misconfigured` /
   `embedding_misconfigured`. Unit tests were added for 404.
2. **Citation markers left in "not in the documents" answers.** The orchestrator strips `[n]`
   from `insufficient_context` answers (`strip_markers`). The unit test now asserts it.
