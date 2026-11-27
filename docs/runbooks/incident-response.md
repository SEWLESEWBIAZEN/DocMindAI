# Runbook: incident response

Each alert in `deploy/monitoring/alerts.yml` links to a section of this runbook. All commands run from
`deploy/production/`. `dm` is short for `docker compose exec api documind-manage`.

## General procedure

1. **Acknowledge** the alert and open an incident note: the time, the alert, and who is handling it.
2. **Scope.** Is the problem everyone, one organization or one feature?
   - `curl -fsS https://$SERVER_NAME/api/v1/health` shows the overall health of each dependency.
   - The request id in user reports (`X-Request-ID`) finds the exact log lines:
     `docker compose logs api | grep <request_id>`.
3. **Mitigate first, then find the root cause.**
4. **Security incidents** (suspected data exposure, leaked key, account takeover): follow
   [Security incident](#security-incident). Preserve logs before restarting anything.
5. **Afterwards:** write a short post-incident review covering the timeline, cause, impact, fix and
   follow-ups. If customer data was affected, notify customers as your contracts and the law require.

## API down

Alert: `DocuMindDown`. Prometheus can't scrape `/api/v1/metrics`.

1. Check the containers with `docker compose ps api web`, then `docker compose logs --tail 200 api`.
2. **Configuration errors on start-up** (`ValueError: ... in production`): a setting was changed.
   Fix `.env`, then `docker compose up -d api`.
3. **Out of memory** (`docker inspect` shows `OOMKilled`): lower `API_WORKERS`, or add memory. The
   baseline in `loadtest/README.md` is 3 workers on 4 vCPU and 8 GB.
4. **Database refusing connections**: see [Dependency down](#dependency-down).
5. **Only the metrics scrape is failing**, and users are fine: check that `METRICS_TOKEN` matches
   Prometheus's `metrics_token` file.

## Dependency down

Alert: `DocuMindDependencyDown`, with label `component` set to `postgres`, `redis` or
`object_storage`.

- **postgres**:
  - Check the provider's status page and connection limits.
  - Pool size is `DATABASE_POOL_SIZE` × `API_WORKERS`, plus the workers. Stay below the server's
    `max_connections`.
  - While the database is down, sign-in and every page fail. Nothing is lost: uploads already stored
    have durable job rows.
- **redis** (also alert `DocuMindRateLimiterDegraded`):
  - The Celery queue and answer cancellation are affected.
  - Rate limiting **stays on** with a per-process fallback, so sign-in, password-reset, 2FA
    and sign-up limits still apply. Each API process counts on its own, so the effective limit
    is up to `API_WORKERS` × the configured limit.
  - **AI answers and speech are refused** with `503 rate_limiter_unavailable` until Redis is
    back: their cost limits can't be enforced across processes.
  - The logs show "Rate limiter degraded" once when this starts and "Rate limiter recovered"
    when it ends.
  - Run `docker compose restart redis`. Redis uses append-only persistence.
  - Queued Celery messages can be lost; the sweeper re-dispatches queued jobs from PostgreSQL, so
    run `dm sweep`.
- **object_storage**:
  - Uploads and downloads fail, while existing search and answers keep working.
  - Check the provider's status, the bucket policy, and whether the credentials expired or were
    rotated.

## Processing backlog

Alert: `DocuMindProcessingBacklog`. The oldest queued job is more than 15 minutes old, or more than
200 jobs are queued.

1. Check that the workers are up (`docker compose ps worker`) and consuming
   (`docker compose logs --tail 100 worker`).
2. Find out what is waiting: `dm jobs --status queued --limit 20`.
3. **Workers are up but idle:** messages were lost from Redis. Run `dm sweep`.
4. **Workers are busy because of a genuine surge** (OCR is CPU-bound): scale with
   `docker compose up -d --scale worker=N`, or raise `WORKER_CONCURRENCY` if CPU is available.
5. **One organization's jobs are stuck while the rest move:** its data plane is probably down (see
   [Data plane down](#data-plane-down)), or it uses customer-hosted processing and its worker
   stopped. In the second case, the organization's Infrastructure page shows when its workers were
   last seen.

## Processing failures

Alert: `DocuMindProcessingFailures`.

1. Run `dm jobs --status failed --limit 50` and `--status dead`. The error code shows the cause.
2. **`embedding_*`, `llm_*` or `provider_quota_exhausted`:** see [AI provider](#ai-provider).
3. **`no_text` on many scanned files:** OCR isn't working. Check `OCR_ENABLED` and
   `OCR_LANGUAGES`, and that Tesseract has those languages. If it started with a release, roll back
   ([`deployment.md`](deployment.md#3-rollback)).
4. **`corrupt_file`, `encrypted_file`, `empty_document`, `too_large` or `unsafe_archive`:** a
   problem with that file. The user sees the reason in the document view. No action is needed
   beyond answering support.
5. After a fix, re-run a job with `dm process <job_id> [--workspace <id>]`.

## AI provider

Alerts: `DocuMindAnswerFailures`; `DocuMindInvalidAIKeys` (informational).

- **Platform key** (`OPENAI_API_KEY`):
  - Check the provider's status page, the key's quota and its billing.
  - Answers fail with a clear message and nothing is corrupted.
  - To rotate the key, update `.env`, then `docker compose up -d api worker`.
- **Workspace keys** (BYOK):
  - When a provider rejects a key, it is marked invalid and the owners are emailed automatically.
  - There is no silent fallback to the platform key; that is intended.
  - No operator action is needed unless a customer asks for help.
- **Rate limiting by the provider (429):**
  - The provider clients retry with backoff (`LLM_MAX_RETRIES`, `EMBEDDING_MAX_RETRIES`).
  - Processing jobs that still fail with `embedding_rate_limited` are retried later.
  - If it persists, lower `WORKER_CONCURRENCY`, which reduces embedding load first.

## AI model unavailable

Alert: `DocuMindAIModelUnavailable`. At start-up the API asked the provider about each configured
model, and the key can't use one of them. Requests that need that model fail every time: answers
for `LLM_MODEL`, processing for `EMBEDDING_MODEL`, figure descriptions for the vision model, voice
for the speech models.

1. `docker compose exec api documind-manage ai check` lists each model and its status
   (`MISSING` = not available to this key). Exit code 1 means at least one is missing.
2. Either set the variable to a model the key can use, or allow the model for the key (OpenAI:
   the project's model limits). Then `docker compose up -d api worker`.
3. Changing `EMBEDDING_MODEL` changes the vectors: re-index afterwards
   (`documind-manage reindex --all`).

## Database slow

Symptom: requests answer `503 database_timeout`. A statement on an API connection ran longer
than `DATABASE_STATEMENT_TIMEOUT_SECONDS` and PostgreSQL cancelled it, so the connection and the
request were freed.

1. Find the slow statements: `SELECT pid, now() - query_start AS age, left(query, 200) FROM
   pg_stat_activity WHERE application_name LIKE 'documind-%' ORDER BY age DESC;`. API, worker
   and CLI connections are named `documind-api`, `documind-worker` and `documind-cli`.
2. Look for lock waits (`wait_event_type = 'Lock'`), a missing index after an upgrade, or a
   database under load from another tenant.
3. Raise the timeout only as a stopgap; the default 30 s is far above any normal request.

## Orphaned objects

`documind-manage orphans` lists objects in the platform bucket that no row refers to. They are
left behind when an object delete fails after the database commit, or when an upload's
transaction fails and so does its clean-up. It only reports; `--delete` removes them.

- Objects newer than 24 hours (`--min-age-hours`) are never counted: an upload may still be
  committing.
- Workspaces being migrated, and those whose storage moved to their own bucket (the retained
  migration source), are skipped.
- Unknown prefixes are listed but never deleted. A customer's own bucket is never touched.
- Run it monthly, or after an incident with storage errors in the logs. Check the report before
  you run `--delete`.

## Data plane down

Alert: `DocuMindDataPlaneDown`. A dedicated or external database is unreachable.

Only the affected organization is impacted. Its data pages answer 503 `infrastructure_unavailable`;
sign-in and other organizations are unaffected.

1. Find the organization: the API log line `data plane unavailable` carries the `version_id`. The
   organization's Infrastructure page shows the history.
2. **Dedicated database:** this is our server. Check it the same way as the main database.
3. **External database:** this is the customer's server.
   - Contact their administrators.
   - Common causes:
     - a firewall change;
     - an expired TLS certificate (`verify-full`);
     - a rotated password, which the owner fixes by saving a new version with the new password;
     - a DNS change that now points to a blocked private address (`infrastructure_blocked` in the
       logs).
4. Jobs wait in that database and are dispatched again once it is back. Nothing needs replaying.

## Migration stuck

Symptom: an organization answers 503 `migration_in_progress` for longer than its size explains, and
`dm migrations list` shows it as `running`.

1. Check that the worker running it is alive: look for `infrastructure migration` in
   `docker compose logs worker`.
2. **Still copying:** let it finish. The time needed grows with the number of chunks and files.
   `dm migrations list` shows `heartbeat_at` and the finished `phases` (`preflight_passed`,
   `rows_copied`, `objects_copied`, `verified`). A live run refreshes its heartbeat between
   tables and every 25 files.
3. **The worker died mid-run** (no heartbeat for more than 5 minutes):
   - The task is never redelivered, so the workspace stays locked.
   - **Preferred: `dm migrations resume <id>`.** It takes over the run and continues from the
     last finished phase:
     - an unfinished row copy left nothing committed (it is one transaction), so it is redone
       from scratch;
     - files already at the destination with the right size are skipped;
     - the copy is verified again before the switch;
     - if the process died after the switch, it only marks the migration completed.
   - `resume` refuses with `migration_active` while the heartbeat is fresh, so two runners never
     work on one migration. If the original process was only stalled, its next heartbeat finds
     the lease gone and it stops **without** clean-up.
   - **To give up instead:** `dm migrations abort <id>` removes the partial copy at the
     destination, marks the migration failed and unlocks the workspace on its **original**
     infrastructure. The source was never modified. The owner can then approve a new plan.
4. **Failed with `already has ... rows`:** the destination holds data for this workspace, for
   example from an earlier attempt that someone restored by hand. It was not deleted. Clear it with
   the customer's agreement before retrying.

## Backup stale

Alert: `DocuMindBackupStale`. See [`backup-and-restore.md`](backup-and-restore.md).

1. Read `docker compose logs --tail 200 backup`.
2. Typical causes:
   - a full backup volume;
   - expired offsite credentials;
   - `pg_dump` and server versions that don't match.
3. Run a backup by hand:

   ```bash
   docker compose --profile backup run --rm -e BACKUP_ONCE=true backup
   ```

   This takes the backup, copies it offsite and updates `documind_backup_last_success_timestamp`,
   exactly like the nightly run.

## Two-factor reset requests

Users who lost every second factor (app, passkeys, recovery codes **and** email) can ask for a reset
from the sign-in screen. Requests are a favourite of attackers who know a password, so:

1. List open requests with `dm mfa-reset list --status pending`. Note the email, the reason, the IP
   address and the time.
2. **Verify identity outside DocuMind.** Never accept anything sent to the account's own email or a
   number or address given in the request itself. Acceptable checks:
   - a video call comparing a government ID with the account's name;
   - for organization members, confirmation from a known admin of their organization, reached
     through contact details you already had;
   - for paying customers, confirmation from the billing contact on file.
3. Approve or deny, recording who verified and how:

   ```bash
   dm mfa-reset approve <id> --operator <you> --note "video call, passport checked"
   dm mfa-reset deny <id> --operator <you> --note "could not verify"
   ```
4. An approval takes effect only after `MFA_RESET_DELAY_HOURS` (72 h). If the real owner signs in
   with any second factor before then, the request is cancelled automatically. Tell the person how
   long they need to wait.

Several requests for one account, or many requests from one address, are signs of an attack. Deny
them and follow [Security incident](#security-incident).

## Verifying an SSO domain

An organization that sets up single sign-on, or changes its issuer, client or domains, waits as
`pending_verification`. Approving a domain lets that organization's identity provider sign in
everyone with an address at that domain, so check ownership first.

1. List connections with `dm sso list`. Note the organization, its domains and its issuer.
2. **Confirm the customer owns each domain.** Acceptable checks:
   - a DNS TXT record you ask them to publish, such as `documind-verify=<connection id>`
     (`dig TXT example.com`);
   - for an existing customer, confirmation from a known admin reached through contact details
     you already had.

   The issuer should belong to the same company or a known identity provider. A tenant URL on a
   mismatched domain needs a second look.
3. Approve or disable, recording who checked and how:

   ```bash
   dm sso approve <id> --operator <you> --note "TXT record checked 2026-11-26"
   dm sso disable <id> --operator <you> --note "domain not owned"
   ```

If an identity provider is compromised, `dm sso disable <id>` stops SSO sign-in at once. Then
revoke the affected sessions and follow [Security incident](#security-incident).

## TLS certificate

Alerts:
- `DocuMindTLSCertificateExpiresSoon` (under 14 days left);
- `DocuMindTLSCertificateExpiresImminently` (under 3 days);
- `DocuMindTLSCertificateSelfSigned` (still on the temporary certificate);
- `DocuMindTLSCertificateUnreadable`.

Let's Encrypt certificates last 90 days, and certbot renews them with 30 days left, retrying
every 12 hours. So an expiry alert means renewal has been failing for over two weeks.

1. **See why it fails:**
   ```
   docker compose logs --tail 200 certbot | grep -iE "renew|error|challenge"
   ```
2. **Common causes:**
   - Port 80 isn't reachable from the internet (firewall, load balancer): the HTTP-01
     challenge needs it.
   - DNS for `SERVER_NAME` no longer points here.
   - Rate limits after many failures: wait, and test with `LETSENCRYPT_STAGING=true` first.
   - `LETSENCRYPT_EMAIL` is unset (the certbot container exits).
3. **Test the challenge path from outside:**
   ```
   curl -i http://$SERVER_NAME/.well-known/acme-challenge/probe
   ```
   Expect `404` from nginx, not a redirect or a timeout.
4. **Renew now:**
   ```
   docker compose exec certbot certbot renew --webroot -w /var/www/acme --force-renewal
   ```
   Then make nginx load the new certificate without dropping connections:
   ```
   docker compose exec web nginx -s reload
   ```
   It also reloads by itself every 6 hours.
5. **Check:** `openssl s_client -connect $SERVER_NAME:443 -servername $SERVER_NAME </dev/null | openssl x509 -noout -enddate`,
   or run `deploy/production/smoke/tls-check.sh $SERVER_NAME 443 80 /etc/ssl/certs/ca-certificates.crt`.

**Self-signed:** the first issuance failed. Fix the cause above, then
`docker compose restart certbot`. It replaces the temporary certificate as soon as Let's
Encrypt answers.

**Unreadable:** the API reads `TLS_CERTIFICATE_FILE` from the `letsencrypt` volume, mounted
read-only. Check that the volume is mounted and that `SERVER_NAME` matches the certificate's
directory name.

## Security incident

1. **Contain.**
   - **Leaked `JWT_SECRET_KEY`:** rotate it, which invalidates every access token. Also delete the
     rows in `refresh_tokens`, or the old sessions can mint new access tokens. Everyone has to sign
     in again.
   - **Leaked platform AI key:** revoke it at the provider and replace it.
   - **Leaked customer credential:** revoke it at the source. Owners save a new infrastructure or
     AI-key version, which revokes the old credential in the vault.
   - **Suspected account takeover:** set `users.is_active = false`, and revoke its sessions by
     deleting its rows in `refresh_tokens`.
2. **Preserve evidence.** Copy the container logs and an `audit_logs` export for the time window
   before restarting containers. Docker rotates logs.
3. **Investigate** with the audit log: actor, IP address, user agent and request id are recorded
   for every sensitive action.
4. **Rotate keys** that may be exposed, following
   [`disaster-recovery.md`](disaster-recovery.md#key-rotation).
5. **Notify** affected customers and authorities within the deadlines that apply. For EU personal
   data under GDPR, that is 72 hours.
