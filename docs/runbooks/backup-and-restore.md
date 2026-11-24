# Runbook: backup and restore

Verified by `backend/tests/integration/test_backup_restore.py` (Phase 9). Scheduling, offsite copies
and monitoring are part of Phase 10.

## What is backed up

| Data | Where | Backed up by |
|------|-------|--------------|
| Control plane (users, workspaces, audit, plans, usage, encrypted credentials, infrastructure registry) | DocuMind PostgreSQL | `backup.sh` (`PGURL`) |
| Shared data plane (documents, chunks, conversations of workspaces on the defaults) | same database | `backup.sh` |
| Dedicated data planes | DocuMind-managed servers | `backup.sh` (`DATAPLANE_PGURLS`, `documind` schema) |
| Document files | DocuMind bucket | `backup.sh` (`documind-manage objects export`) |
| External databases and customer buckets | the customer | the customer (their infrastructure, their backups) |
| `CREDENTIAL_ENCRYPTION_KEYS`, `MFA_ENCRYPTION_KEY`, `JWT_SECRET_KEY` | the secrets manager | **separately**: never inside an archive |

## Security properties

- Archives are always encrypted: AES-256-GCM in authenticated 1 MiB chunks, with the key derived from a
  passphrase using scrypt. A wrong passphrase, tampering, reordering or truncation is detected before
  anything is restored.
- Files are created with mode 0600. Archives carry SHA-256 checksums of every file, and the document
  export has its own manifest.
- The archive contains customer credentials only as ciphertext (ADR 0022). A stolen archive plus its
  passphrase still doesn't reveal AI keys or database passwords without the separately stored key ring.
  The test asserts that no document text, email address or key appears in the archive bytes.
- Restores refuse a non-empty target database. Data is never restored over live data.

## Take a backup

```bash
export PGURL=postgresql://documind:...@db-host:5432/documind      # libpq form (no +asyncpg)
export BACKUP_DIR=/var/backups/documind
export BACKUP_PASSPHRASE_FILE=/run/secrets/backup_passphrase        # >= 16 characters
export DATAPLANE_PGURLS="postgresql://dm_abc_app:...@dedicated-host:5432/dm_abc"   # optional
infra/backup/backup.sh            # writes documind-<UTC timestamp>.tar.dmbk, prunes > RETENTION_DAYS
```

It needs `pg_dump` (version 16 or later), `tar`, `sha256sum` and `documind-manage`, with S3 settings for
the DocuMind bucket.

## Restore (drill or disaster)

1. Create an **empty** database (with pgvector available) and an empty private bucket.
2. Run:

   ```bash
   export PGURL=postgresql://...@new-host:5432/documind_restored
   export S3_BUCKET=documind-restored BACKUP_PASSPHRASE_FILE=/run/secrets/backup_passphrase
   infra/backup/restore.sh /var/backups/documind/documind-20261124T080000Z.tar.dmbk
   alembic upgrade head && documind-manage check
   ```
3. Start the API and workers with the restored `DATABASE_URL`, the restored `S3_BUCKET`, and the
   **original** `CREDENTIAL_ENCRYPTION_KEYS`, `MFA_ENCRYPTION_KEY` and `JWT_SECRET_KEY`.
4. Verify:
   - sign in;
   - open a document and download it;
   - run a search;
   - for a workspace with its own AI key, confirm that **AI providers** shows it as active.

Run a restore drill at least quarterly, and after every change to the backup scripts.

## Key ring rotation and backups

Old archives hold credentials wrapped with the keys that were current when they were taken. Keep retired
key-ring entries in the secrets manager for as long as you keep archives that need them, or re-enter
customer credentials after restoring.
