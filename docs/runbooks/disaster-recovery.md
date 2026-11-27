# Runbook: disaster recovery

This runbook builds on [`backup-and-restore.md`](backup-and-restore.md), which covers taking backups
and restoring them, and has been verified by a restore drill. This page covers deciding what to
restore, in which order, and how to rotate keys.

## Objectives

| | Default setup | With managed PostgreSQL point-in-time recovery (recommended) |
|---|---|---|
| **RPO** (data loss) | up to 24 h (nightly encrypted archive) | minutes for the database; files up to 24 h unless the bucket is versioned or replicated |
| **RTO** (time to restore) | about 1–2 h for tens of GB, mostly download and `pg_restore` time | about 1 h |

Measure your own RTO in the quarterly restore drill, and write it here.

**Who backs up what:**

- DocuMind backs up the control plane, the shared data plane, dedicated data planes and the
  DocuMind bucket.
- **Customers back up their own external databases and buckets.** DocuMind never holds copies of
  them, which is the point of those modes.

## What must survive outside the archives

Store these in a secrets manager, in a different account or region from the servers:

| Secret | If lost |
|--------|---------|
| `CREDENTIAL_ENCRYPTION_KEYS`, every entry still needed by any archive | Workspace AI keys and database or storage credentials can't be decrypted. Customers must re-enter them |
| `MFA_ENCRYPTION_KEY` | 2FA secrets can't be decrypted. Every user with 2FA must use a recovery code or be reset |
| `JWT_SECRET_KEY` | Nothing lasting: sessions end and users sign in again |
| Backup passphrase (`secrets/backup_passphrase`, including old ones) | Those archives can never be decrypted |
| `.env` | Rebuild it from `.env.example` and the secrets manager |

## Scenarios

### Host lost, data services intact (managed database and S3)

1. Provision a new VM, then install Docker and the repository at the deployed release tag.
2. Restore `.env` and `secrets/` from the secrets manager.
3. Bring the stack up with the same command as before ([`deployment.md`](deployment.md#1-first-deployment)).
   Certificates are issued again automatically.
4. Update DNS if the address changed.

Data loss: none. Redis held only queues, rate limits and caches, and the sweeper re-dispatches jobs.

### Database lost or corrupted

1. **Prefer the provider's point-in-time recovery** to just before the incident, restored into a
   **new** instance.
2. Otherwise, restore the latest archive into a new, empty database:
   [`backup-and-restore.md`](backup-and-restore.md#restore-drill-or-disaster).
3. Point `DATABASE_URL` at it, run `docker compose run --rm migrate`, then start the stack.
4. **Files uploaded after the archive was taken** exist in the bucket without rows. They are
   harmless. **Rows newer than the files** cannot occur: files are written before their rows.

### Bucket lost

Restore the files in the archive with `documind-manage objects import`, or recover from the
provider's versioning or replication.

Documents whose file is missing still show and search, because text and chunks live in the
database, but downloads fail. List them by comparing `document_versions.storage_key` with the bucket.

### Dedicated data plane lost

1. Restore its `documind` schema from the archive (the `DATAPLANE_PGURLS` dumps) into a database
   with the **same name**, on a server reachable at the **same host**
   (`DEDICATED_DATABASE_HOST`).
2. Recreate its role with the same password. The password is in the credential vault, and the
   role name is in the version's configuration.
3. Run `documind-manage dataplanes migrate`, then check that the organization's pages load.

Owners can't change a dedicated version's host. Moving a dedicated plane to a different host needs
a new version created by provisioning. There is no operator tool for that yet; it is a known gap.

Only that organization is affected meanwhile: it gets 503 `infrastructure_unavailable`.

### Customer's external database or bucket lost

The customer restores from **their own** backups to the same address, or to a new one that they
then register as a new version. DocuMind's data for them consists only of the control-plane records
(membership, usage and audit), which DocuMind's own backups cover.

### Whole region lost

1. Restore the latest archive from the **offsite** bucket into a database and bucket in another
   region.
2. Deploy the stack there with the same secrets.
3. Move DNS.

Customers with external infrastructure are unaffected, as long as it remains reachable from the new
region.

## After any restore

- Run `documind-manage check`.
- Sign in, search, download, and ask a question.
- Check a BYOK workspace's **AI providers** page.
- `documind-manage jobs --status running`: running jobs from before the incident are recovered by
  the sweeper once their lease expires.
- Record the incident and the real RPO and RTO.

## Key rotation

Rotate on a schedule (yearly), when people with access leave, and immediately after a suspected
leak.

| Key | How | Effect |
|-----|-----|--------|
| `CREDENTIAL_ENCRYPTION_KEYS` | 1. Prepend a new `id:key` entry, keeping the old ones. 2. Restart `api` and `worker`. 3. Run `documind-manage credentials rewrap`. 4. Remove the old entry only once no archive you keep needs it | None for users |
| `JWT_SECRET_KEY` | Replace it, restart, and delete the rows in `refresh_tokens` | Everyone signs in again |
| `MFA_ENCRYPTION_KEY` | Key ring: set the new key as `MFA_ENCRYPTION_KEY`, move the old one to `MFA_ENCRYPTION_KEYS_PREVIOUS`, restart, then `documind-manage mfa rewrap --dry-run` and `mfa rewrap` (see below) | Nobody: authenticator codes keep working throughout. Recovery codes issued under the old key work while it stays in `MFA_ENCRYPTION_KEYS_PREVIOUS` |
| `REDIS_PASSWORD` | Replace it, then `docker compose up -d` (Redis and every client restart) | Seconds of downtime; queued messages may need `documind-manage sweep` |
| `METRICS_TOKEN` | Replace it in `.env` and in Prometheus's `metrics_token` file, then restart `api` | None |
| Backup passphrase | Write the new one to `secrets/backup_passphrase`. New archives use it | **Keep the old passphrase** for as long as older archives are retained |
| Platform AI key, SMTP, S3 credentials | Replace them at the provider and in `.env`, then restart `api`, `worker` and `beat` | None |
| Customer credentials (BYOK keys, external database password, storage keys) | Owners save a new AI key or infrastructure version. The old credential is revoked and its ciphertext destroyed | None |
| Customer worker tokens | The owner registers a new worker, deploys its token, then revokes the old one | None |

### Rotating `MFA_ENCRYPTION_KEY` without locking anyone out

1. **Generate a key:**
   ```
   python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
   ```
2. **Update `.env`:**
   ```
   MFA_ENCRYPTION_KEY=<new key>
   MFA_ENCRYPTION_KEYS_PREVIOUS=<old key>   # several: comma-separated, newest first
   ```
   Restart `api`, `worker` and `beat`. New secrets are encrypted with the new key
   (`v2:<key id>:…`, where the key id is the first 8 hex characters of SHA-256 of the key). Old
   ones are still read with the old key.
3. **Re-encrypt the stored secrets:**
   ```
   docker compose exec api documind-manage mfa rewrap --dry-run   # counts only
   docker compose exec api documind-manage mfa rewrap
   ```
   It prints counts and user ids only, never secrets. It's safe to run again: secrets already
   on the current key are skipped. Exit code 1 means some secrets couldn't be decrypted with
   any configured key; their user ids are listed. Those users need a support reset
   (ADR 0030).
4. **Decide when to drop the old key.** Recovery codes and emailed codes are keyed hashes
   and can't be re-keyed:
   - **Keep the old key** in `MFA_ENCRYPTION_KEYS_PREVIOUS` until everyone who matters has
   regenerated their recovery codes (Profile → Security). It's then used only to check those
   codes.
   - **After a leak, remove it at once.** Authenticator apps and passkeys keep working;
   recovery codes issued before the rotation stop working.
5. **Back up the new key** with the other secrets (see "What must survive outside the
   archives").

**Rollback.** Put the old key back as `MFA_ENCRYPTION_KEY` and the new one in
`MFA_ENCRYPTION_KEYS_PREVIOUS`, then restart: everything stays readable. Run `rewrap` again
if you want the old key to be primary everywhere.

Verified by `test_rotating_the_mfa_key_keeps_everyone_signed_in`: enrol, rotate, sign in
with an app code and a recovery code, dry run, rewrap (idempotent), retire the old key.
A key removed too early gives `503 mfa_unavailable`, not a crash.

