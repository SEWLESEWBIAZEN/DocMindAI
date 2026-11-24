# Security review (Phase 9)

- Date: 2026-11-24
- Scope: the whole application at commit `39a43f7` and later. That covers identity and sessions,
  tenancy, documents and processing, RAG and conversations, voice, plans and quotas, BYOK, the
  credential vault, and infrastructure independence (dedicated and external databases, customer
  storage, worker registrations).
- Method: manual code review against OWASP ASVS L2 themes; tests written to prove each property
  (listed as evidence); dependency audits.
- This is an internal review. An independent penetration test is still recommended before general
  availability. See "Open" below.

## Dependency audits

| Tool | Scope | Result |
|------|-------|--------|
| `pip-audit` | 192 locked runtime packages (`uv export --no-dev`) | No known vulnerabilities |
| `npm audit --omit=dev` | Frontend production dependencies | 0 vulnerabilities |
| Ruff `S` (bandit) rules | Backend source, in CI | Clean (justified `noqa`s only) |

## Findings fixed in this phase

| # | Severity | Finding | Fix | Evidence |
|---|----------|---------|-----|----------|
| 1 | Medium | API responses had no defensive headers (sniffing, framing, caching of per-user JSON, HSTS) | `SecurityHeadersMiddleware`: nosniff, `X-Frame-Options: DENY`, `default-src 'none'; frame-ancestors 'none'`, `Cache-Control: no-store`, Referrer-Policy, CORP; HSTS in production | `test_security_headers`, `test_hsts_in_production` |
| 2 | Medium | 422 responses echoed submitted values (passwords, API keys) | Validation handler returns only `type`, `loc` and `msg` | `test_validation_errors_do_not_echo_input` |
| 3 | Medium | Logs could carry secrets: provider errors include (masked) keys, and connection errors can contain URLs | `redact()` in both log formatters, covering messages, extras and tracebacks | `test_formatters_redact_*`; integration: no secret in captured logs |
| 4 | Medium | Validation endpoints connect to customer-chosen hosts, so they could be used as a probe or port scanner | Per-workspace outbound limit (20 per hour) on key validation and infrastructure drafts and validation | `test_outbound_checks_are_rate_limited` |
| 5 | Medium | Customer hosts were checked against the SSRF policy only at validation time (DNS rebinding) | Re-checked whenever a data plane or storage adapter is built | `test_customer_hosts_are_rechecked_before_connecting` |
| 6 | Low | An unreachable customer database surfaced as a 500, and crashed worker tasks | 503 `data_plane_unavailable` for that organization only; jobs return `unavailable` and are swept again later | `test_one_organizations_database_outage_is_contained` |
| 7 | Low | Rotated infrastructure secrets stayed decryptable | Credentials used only by the retired version are revoked and destroyed | `test_rotating_the_database_password_needs_no_migration` |
| 8 | Low (a11y) | "Operational" badges failed contrast in light mode | Badge text uses the strong tones | `e2e/a11y.spec.ts` |

## Properties verified (tests)

- **Tenant isolation:**
  - Non-members get 404 on every workspace route (Phases 2–7).
  - Two dedicated databases stay isolated from each other.
  - A data plane rejects rows for unregistered workspaces (tenant anchor FKs).
  - Cross-organization document and download-URL access returns 404.
- **Secrets:**
  - The AI key, database passwords and storage secret are absent from logs, API responses, the audit
    log, infrastructure history, validation results and a `pg_dump` backup.
  - Credentials are envelope-encrypted and bound to their row, with key-ring rotation (rewrap) and
    revocation that destroys the ciphertext.
- **No silent platform fallback:** a rejected workspace key never uses DocuMind's key.
- **Infrastructure integrity:**
  - A tampered profile version is refused with `infrastructure_integrity_error`.
  - Versions are immutable and pinned per workspace.
  - Moving data is refused without a migration, so nothing moves.
- **SSRF:**
  - Blocked: private, loopback, link-local, metadata, CGNAT and IPv4-mapped addresses, plus plain HTTP
    and URL credentials.
  - Operator allowlist only.
- **Backups:** archives are encrypted and authenticated, reject tampering and truncation, contain no
  plaintext, and restore end to end.
- **Authentication (earlier phases):**
  - Argon2id passwords; HS256 JWTs with a pinned algorithm.
  - Hashed refresh, reset and invitation tokens, with refresh rotation and reuse detection.
  - TOTP 2FA with encrypted secrets and step-up for sensitive actions.
  - CSRF header with SameSite=Strict on the refresh cookie; rate limits on login, auth, 2FA, AI and
    voice.
- **Uploads (Phase 3):** type sniffing, size limits, optional ClamAV, private bucket, and signed URLs
  that always download with a fixed content type.
- **LLM safety (Phases 5–6):** retrieved text is neutralized before prompting, citations are verified,
  and Markdown is rendered without HTML.

## Accepted risks (documented)

- **DNS rebinding window.** Hosts are re-checked when connections are built, but asyncpg and boto3
  resolve again when they connect. Pinning the resolved IP needs TLS SNI handling; this is planned with
  the external-database CA work.
- **Quota overshoot by about one request under concurrency** (ADR 0020).
- **Secrets are delivered as environment variables.** Moving to a KMS or Vault adapter behind
  `KeyEncryptionProvider` is a Phase 10 decision.
- **Rejected-key emails** are sent once per transition to invalid, not repeatedly.

## Open (require people or later workstreams)

- An independent penetration test, including tenant breakout through dedicated and external data planes.
- **MI-7:** the worker connector protocol isn't built, so worker authentication and revocation are
  verified only at the registration and token level (hashed, revocable).
- **MI-8:** migration execution safety; there is no execution yet.
- The frontend CSP and security headers depend on Nginx (Phase 10). The API's own CSP is `default-src
  'none'`.
