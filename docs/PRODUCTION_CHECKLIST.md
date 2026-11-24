# Production security checklist

Complete this before the first customer and after every infrastructure change. Each item says how
to check it.

- "Enforced" means the application refuses to start without it.
- Background is in [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md) and
  [`runbooks/deployment.md`](runbooks/deployment.md).

## Secrets

- [ ] **Generated, not hand-typed.** `generate-secrets.sh` creates `JWT_SECRET_KEY`,
  `MFA_ENCRYPTION_KEY`, `CREDENTIAL_ENCRYPTION_KEYS`, `REDIS_PASSWORD`, `METRICS_TOKEN` and the
  backup passphrase. Placeholders from `.env.example` are **enforced** as rejected.
- [ ] **Stored in a secrets manager** in another account or region: the keys,
  `secrets/backup_passphrase` and every retired key-ring entry still needed by archives.
- [ ] **Locked down on the host.** `.env` has mode 0600 and `secrets/` has mode 0700, both owned by
  the deploy user. Neither is in git: `git check-ignore deploy/production/.env` prints it.
- [ ] **Least privilege.** The database role owns only the DocuMind database and is not a superuser.
  The S3 credentials are limited to the one bucket. The offsite backup credentials can write but
  not delete.
- [ ] **Separate keys per environment.** Staging and production never share keys or buckets.

## Network and TLS

- [ ] **Only 80 and 443 are published.** Check with `docker compose ps`, and run an external port
  scan of the host.
- [ ] **A valid certificate is served:**
  `curl -vI https://$SERVER_NAME 2>&1 | grep -E 'subject|expire'`. Renewal is automatic. Alert on
  certificate expiry with the blackbox probe.
- [ ] **Security headers are present:** HSTS, CSP, `X-Content-Type-Options`, `Referrer-Policy` and
  frame denial. Check with `curl -sI https://$SERVER_NAME/`.
- [ ] **Metrics are not public.** `curl -s -o /dev/null -w '%{http_code}' https://$SERVER_NAME/api/v1/metrics`
  returns `404`. Prometheus scrapes it inside the network, with `METRICS_TOKEN`.
- [ ] **Encrypted connections to the database** (`sslmode=verify-full` for managed databases) and
  to S3 (https). SMTP uses STARTTLS or TLS, **enforced**.
- [ ] **`INFRASTRUCTURE_ALLOWED_PRIVATE_HOSTS` is empty**, or lists only exact internal hosts that
  are meant for customer infrastructure (on-premises).
- [ ] **CORS** names only `https://$SERVER_NAME`. Wildcards are **enforced** as rejected.

## Application

- [ ] `ENVIRONMENT=production`: this turns on the production checks and secure cookies.
- [ ] **2FA** is enabled on every operator and owner account. Sensitive actions require a step-up
  anyway: deletions, infrastructure changes, migrations and AI keys.
- [ ] **Malware scanning** is on (`MALWARE_SCANNER=clamav`), or the risk is accepted in writing.
- [ ] **Rate limiting** is on (`RATE_LIMIT_ENABLED` is not `false`), and Redis has a password.
- [ ] **Upload and quota limits** are set to the plan catalogue (ADR 0020). Check with
  `documind-manage plan list`.
- [ ] **AI provider keys** have spending limits at the provider.

## Containers and host

- [ ] **Images are pinned** to release tags, not `latest`, and scanned for vulnerabilities, for
  example with `trivy image`.
- [ ] **App containers are hardened:** non-root, a read-only root filesystem, `cap_drop: ALL` and
  `no-new-privileges`, as the Compose file defines. Check with
  `docker inspect <container> --format '{{.HostConfig.ReadonlyRootfs}} {{.Config.User}}'`.
- [ ] **The host is maintained:** automatic security updates, SSH keys only, a firewall, and the
  clock synced with NTP (TOTP and token expiry depend on it).
- [ ] **Docker logs rotate** (set in the Compose file) and are shipped to a log store with
  retention that matches your policy. Logs redact secrets, but they contain email addresses and IP
  addresses.

## Data protection

- [ ] **Nightly encrypted backups** run, and the **offsite copy** exists. The
  `DocuMindBackupStale` alert is wired up.
- [ ] **A restore drill passed** within the last quarter
  ([`runbooks/backup-and-restore.md`](runbooks/backup-and-restore.md)).
- [ ] **The bucket is private.** Check the public-access block, and that versioning or replication
  is on if your RPO for files is shorter than 24 h.
- [ ] **Dedicated data planes** are listed in `DATAPLANE_PGURLS` for backups.
- [ ] **External customers** were told in writing that they back up their own database and bucket.

## Monitoring and response

- [ ] **Prometheus is configured.** It loads `deploy/monitoring/alerts.yml`, and the alerts reach a
  person on call.
- [ ] **External uptime is probed** on `/api/v1/health`.
- [ ] **The runbooks have been read** by everyone on call:
  [`incident-response.md`](runbooks/incident-response.md) and
  [`disaster-recovery.md`](runbooks/disaster-recovery.md).
- [ ] **Security contacts** are set and a **breach-notification process** is agreed.

## Before launch, outside this repository

- [ ] **An independent penetration test**, recommended in `SECURITY_REVIEW.md`.
- [ ] **Screen-reader testing and blind-user testing**
  ([`accessibility/`](accessibility/)). The protocols are ready, but not yet run with real users.
- [ ] **Legal documents:** privacy policy, terms, data processing agreement, and a sub-processor
  list (AI provider, email, storage, hosting).
