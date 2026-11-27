# ADR 0025: Approved infrastructure migrations and Azure Blob Storage (MI-8, MI-6)

- Status: Accepted
- Date: 2026-11-24
- Builds on: ADR 0021 (infrastructure profiles and data planes), ADR 0022 (credential vault)
- Completes: the MI-8 migration foundation in ADR 0021, which recorded plans without running them

## Context

ADR 0021 refuses to activate a version that moves a non-empty workspace to another database or
bucket (`409 migration_required`) and records a **planned** migration instead. Organizations still
need a way to actually move, for example from DocuMind's shared database to their own PostgreSQL.

The rules for this work are strict:

- no customer data moves without explicit approval;
- a failure must never lose or corrupt data;
- the source must stay recoverable.

Azure Blob Storage (MI-6) was listed as `planned` until an adapter could be tested for real.

## Decision

### Approval is the consent

- An owner approves a planned migration with a fresh 2FA step-up:
  `POST /infrastructure/migrations/{id}/approve`.
- Approval is refused in these cases:
  - the target version is not `validated`;
  - the workspace's infrastructure changed since the plan was made (`migration_plan_stale`);
  - another migration is approved or running (`migration_busy`).
- Activating a version while a migration is approved or running is refused as well.
- Approval queues the Celery task `documind.run_infrastructure_migration`. If the queue is down, an
  operator runs it with `documind-manage migrations run <id>`.

### The executor

The executor lives in `application/infrastructure/migration.py` and uses only the existing ports:
data-plane sessions and `ObjectStorageProvider`. It works in these steps:

1. **Lock.**
   - The migration is set to `running`.
   - The resolver then raises `503 migration_in_progress` for the workspace's data.
   - The infrastructure settings page keeps working.
   - Workers leave its jobs queued: the orchestrator returns `unavailable` before claiming.
   - The executor waits out every process's routing cache (`TENANT_SETTINGS_CACHE_SECONDS + 1`) and
     any running processing jobs.
2. **Preflight.** The destination must hold no rows and no objects for this workspace
   (`DestinationNotEmptyError`). This is what makes the rollback safe: it can only delete what this
   run copied. It never deletes rows that were already there, including a retained copy from an
   earlier move.
3. **Copy.**
   - Tables are copied in dependency order (`DATA_PLANE_TABLES`), in batches of 500.
   - Computed columns (`document_chunks.search_tsv`) are left out; the destination recomputes them.
   - `documents.current_version_id` is linked after the versions exist.
   - Messages are copied in conversation order because of `reply_to_id`.
   - The tenant is registered in an isolated destination first.
   - Objects are copied key by key, with content type and metadata, through the storage ports. A
     migration can therefore cross providers, for example from S3 to Azure.
4. **Verify.** Per-table row counts and the size of every object must match. Writes that slipped in
   after the copy make the counts differ, and the run fails.
5. **Switch.** The workspace is pinned to the target version, the previous version is retired, and
   both an event and an audit entry are recorded.
6. **Retain.** The source copy and its credentials are kept.
   `POST /migrations/{id}/cleanup` (owner, fresh 2FA) deletes them, then revokes the credentials only
   the old location used. Cleanup is refused if the workspace has moved again since
   (`migration_superseded`).

**On failure:**

- the partial copy is removed (only if preflight passed);
- the migration is set to `failed` with the error;
- the workspace unlocks on its original infrastructure.

**If the process dies mid-run:** the task is not `acks_late`, so it is never redelivered half-done.
The migration stays `running`, and the workspace stays locked, until an operator acts.

*Amended (production readiness F3):* a run now holds a lease (`plan.runner`, `plan.heartbeat_at`)
and records each finished phase. `documind-manage migrations resume <id>` takes over once the
heartbeat is more than 5 minutes old and continues from the last finished phase, verifying again
before the switch. `documind-manage migrations abort <id>` still removes the partial copy and marks
the migration failed.

### Azure Blob Storage (MI-6)

The adapter is `infrastructure/storage/azure_provider.py`, using `azure-storage-blob`. It runs the
synchronous SDK off the event loop, like the S3 adapter.

**Mapping onto the storage profile:**

| Profile field | Azure meaning |
|---|---|
| `bucket` | Container. Validated against Azure naming rules. |
| `access_key_id` | Storage account name. |
| Vaulted secret | Account key. |
| `endpoint_url` | Always stored explicitly; the default is `https://<account>.blob.core.windows.net`. |

Storing the endpoint explicitly means the SSRF policy always checks the real host.

**Endpoint paths.** The SSRF policy still refuses endpoint paths, with one exception for Azure: the
path may be exactly `/<account>`. This is the path-style endpoint that Azure Stack and the Azurite
emulator use.

**Downloads** use read-only service SAS URLs that force an attachment and the content type we chose,
the same contract as S3 presigned URLs.

**Validation:**

- write, read and delete a probe object;
- refuse containers with anonymous access (`bucket_public`);
- report a wrong key as `storage_rejected`.

## Consequences

- Moving a workspace means downtime for that workspace only, roughly proportional to its data.
  Other workspaces are unaffected.
- The copy runs in one process and does not resume. A large workspace that fails restarts from the
  beginning, which is safe because the destination was cleaned.
- During the retention window, data exists in two places. Owners see "the old copy is kept until you
  delete it" and decide when to delete it.
- Azure authentication is by account key only. Microsoft Entra ID (managed identity) and
  user-delegation SAS are not implemented.

## Verification

All of these run against real PostgreSQL, MinIO and Azurite.

`tests/integration/test_infrastructure_migration.py`:

- moving from shared to an external database plus a customer bucket with documents, chunks and a
  conversation;
- the lock;
- search, history and downloads after the switch;
- retention, then cleanup;
- a destination that is not empty;
- a failure mid-copy, which removes the partial copy.

`tests/integration/test_azure_storage.py`:

- processing, search and SAS downloads on Azure;
- a wrong key, and a public container;
- a migration from DocuMind's storage to Azure.
