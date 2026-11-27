# Runbook: deployment, upgrade and rollback

This runbook uses the production stack in `deploy/production/`, which consists of:

- `web`: Nginx with TLS, serving the frontend;
- `api`;
- `worker`;
- `beat`;
- `redis`;
- optionally `postgres`;
- `certbot`;
- `backup`.

**Target host:** one Linux VM with 4 vCPU, 8 GB RAM and Docker Engine 24 or later with Compose v2.
The load-test baseline is in `loadtest/README.md`.

Before going live, also complete [`../PRODUCTION_CHECKLIST.md`](../PRODUCTION_CHECKLIST.md).

## 1. First deployment

### Prerequisites

- A DNS `A` or `AAAA` record for `SERVER_NAME` pointing to the host.
- Ports 80 and 443 open to the internet. Nothing else needs to be exposed.
- PostgreSQL 16 with pgvector. Managed is recommended; otherwise use the `bundled-db` profile.
- A private bucket on an S3-compatible service (ADR 0023), plus a separate offsite backup bucket.
- An SMTP relay with STARTTLS.
- An OpenAI API key, unless every workspace brings its own key.

### Steps

1. **Configure.**

   ```bash
   cd deploy/production
   cp .env.example .env
   ./generate-secrets.sh >> .env    # JWT, MFA and credential keys, Redis password, metrics token
   chmod 600 .env
   $EDITOR .env                     # every CHANGE_ME; see docs/CONFIGURATION.md
   ```

   - Copy the generated secrets into your secrets manager **now**:
     - `JWT_SECRET_KEY`;
     - `MFA_ENCRYPTION_KEY`;
     - `CREDENTIAL_ENCRYPTION_KEYS`;
     - `secrets/backup_passphrase`.
   - Losing `MFA_ENCRYPTION_KEY` or `CREDENTIAL_ENCRYPTION_KEYS` makes 2FA secrets and customer
     credentials unreadable. Backups do not contain these keys.
   - The backend refuses to start in production with any of these:
     - placeholder values;
     - plain-HTTP `PUBLIC_APP_URL`;
     - wildcard CORS;
     - insecure cookies;
     - SMTP without TLS;
     - no credential key ring.

2. **Build or pull the images.** Pin release tags rather than `latest`:

   ```bash
   export DOCUMIND_IMAGE=registry.example.com/documind-backend:1.0.0
   export DOCUMIND_WEB_IMAGE=registry.example.com/documind-web:1.0.0
   export DOCUMIND_BACKUP_IMAGE=registry.example.com/documind-backend:1.0.0-backup
   docker compose build      # or: docker compose pull
   ```

   - Images built from this repository use these targets:
     - `backend`: `runtime`, `backup` and `customer-worker`;
     - `frontend`: `runtime`.
   - The backend build fails if the Celery transport, the application or the CLI can't be imported.

3. **Start.** Leave out `bundled-db` if you use a managed database.

   ```bash
   docker compose --profile letsencrypt --profile backup up -d
   docker compose logs -f migrate     # alembic upgrade head, bucket bootstrap, data planes
   ```

   - `web` starts with a temporary self-signed certificate.
   - `certbot` then obtains the Let's Encrypt certificate through the webroot challenge, and `web`
     reloads every 6 hours.
   - Set `LETSENCRYPT_STAGING=true` for a first dry run.

4. **Verify.**

   ```bash
   curl -fsS https://$SERVER_NAME/api/v1/health        # {"status":"ok", ...}
   docker compose exec api documind-manage check       # database, Redis, storage
   docker compose ps                                   # all services healthy
   ```

   Then check these by hand:
   - Sign up. The verification email arrives.
   - Enable 2FA, upload a PDF, wait for **Ready**, ask a question, and download the file.
   - Response headers include `Strict-Transport-Security`, `Content-Security-Policy` and
     `X-Content-Type-Options`.
   - `https://$SERVER_NAME/api/v1/metrics` returns 404 from outside, because it is internal only.

5. **Assign plans** to organizations as an operator:

   ```bash
   docker compose exec api documind-manage plan assign <workspace_id> business
   ```

6. **Monitoring.** Run Prometheus with `deploy/monitoring/prometheus.yml` and `alerts.yml` on the
   stack's `internal` network:
   - put the `METRICS_TOKEN` value in `/etc/prometheus/metrics_token`;
   - route the alerts. Each alert links to a section of
     [`incident-response.md`](incident-response.md).

7. **Backups.** Confirm the first nightly run (`docker compose logs backup`). Run a restore drill
   ([`backup-and-restore.md`](backup-and-restore.md)) before onboarding customers.

### Reproducing the deployment locally

`deploy/production/smoke/run.sh up && run.sh test` starts this exact stack in production mode. It
runs on `https://localhost:8443` with a throwaway CA, Mailpit and an OpenAI stub, then runs the
Playwright journey against it. Use it to rehearse an upgrade before doing it in production.

## 2. Upgrade

Upgrades are rolling for stateless services. Schema migrations run once, in `migrate`, before the
new API starts.

1. **Read the release notes** for:
   - schema migrations;
   - new settings, added to `.env` first;
   - data-plane migrations (`alembic_dataplane`).
2. **Back up** right before the upgrade:

   ```bash
   docker compose --profile backup run --rm -e BACKUP_ONCE=true backup
   ```

3. **Rehearse** with the smoke stack, or a staging copy restored from that backup, if the release
   has migrations.
4. **Upgrade:**

   ```bash
   export DOCUMIND_IMAGE=registry.example.com/documind-backend:1.1.0   # and WEB / BACKUP
   docker compose pull
   docker compose run --rm migrate          # control plane, bucket, every data plane
   docker compose up -d api worker beat web
   ```

   `migrate` runs `documind-manage dataplanes migrate`, which handles three cases:
   - **dedicated databases** are upgraded;
   - **external databases whose owners allowed it** (`manage_schema`) are upgraded;
   - **external databases without consent** are listed but not touched. Tell those customers to run
     `alembic -n dataplane upgrade head` before you switch, or their workspace answers 503 until
     they do.

   **Customer workers** must run the same release. Tell customers with customer-hosted processing to
   update their worker image.
5. **Verify** as in step 4 of the first deployment, and watch the alerts for 30 minutes.

During `up -d` the API is unavailable for a few seconds while its container restarts. For zero
downtime, run two API replicas behind the proxy and replace them one at a time.

## 3. Rollback

### Application only, no schema change in the release

```bash
export DOCUMIND_IMAGE=registry.example.com/documind-backend:1.0.0   # previous tags
docker compose up -d api worker beat web
```

### Release with a schema migration

Migrations are written to be backwards compatible for one release: they add columns or tables and
never drop them in the same release. Rolling the application back is therefore normally safe
**without** downgrading the schema.

Downgrade only if the release notes say so:

```bash
docker compose run --rm migrate alembic downgrade <previous_revision>   # replaces the default command
```

Downgrades can drop columns added by the release, together with the data in them.

### Last resort: data damaged by a release

Restore the pre-upgrade backup into a **new** database and bucket, following
[`backup-and-restore.md`](backup-and-restore.md), and point the stack at it. Never restore over the
live database. Data written after the backup is lost, so compare the audit log before deciding.

## 4. Routine operations

| Task | Command |
|------|---------|
| Health of dependencies | `docker compose exec api documind-manage check` |
| Stuck or failed processing jobs | `documind-manage jobs --status failed`, then `documind-manage sweep` |
| Re-run one job | `documind-manage process <job_id> [--workspace <id>]` |
| Re-index after an embedding model change | `documind-manage reindex --workspace <id>` |
| Check answer quality (before a release, after a model or prompt change) | Load the sample documents into a test workspace, then `documind-manage eval <workspace_id> --min-pass 0.9 --markdown eval.md`; exit code 1 means a regression ([ADR 0038](../adr/0038-answer-feedback-and-quality-checks.md)) |
| Rotate the credential key ring | Add the new key **first** in `CREDENTIAL_ENCRYPTION_KEYS`, restart, then run `documind-manage credentials rewrap`. Keep the old key while backups need it ([`disaster-recovery.md`](disaster-recovery.md#key-rotation)) |
| Infrastructure migrations | `documind-manage migrations list`, then `run <id>`, `resume <id>` or `abort <id>` ([`incident-response.md`](incident-response.md#migration-stuck)) |
| Certificates | Automatic. Force a renewal with `docker compose --profile letsencrypt run --rm --entrypoint certbot certbot renew --force-renewal` |
| Logs | JSON on stdout, rotated by Docker (20 MB × 5 per service). Ship them to your log system. Secrets are redacted |
