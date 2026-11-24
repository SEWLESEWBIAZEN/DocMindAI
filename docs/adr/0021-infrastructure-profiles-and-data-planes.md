# ADR 0021: Infrastructure profiles, resolution and data planes

- Status: Accepted
- Date: 2026-11-24
- Phase: 8 (MI-1 to MI-5), with foundations for MI-6 to MI-8
- Related: ADR 0006 (local object storage), 0009 (tenancy), 0012 (document storage), 0014 (processing),
  0022 (credential vault)

## Context

The product owner approved infrastructure independence per organization:

- **Databases:** DocuMind shared, dedicated (DocuMind-managed) or external (customer-managed)
  PostgreSQL.
- **Object storage:** DocuMind-managed, AWS S3, S3-compatible, MinIO, and later Azure Blob.
- **Processing:** DocuMind-hosted, customer-hosted or hybrid workers.

Choices are independent and may be mixed. Identity, membership and authorization must stay with
DocuMind.

## Decision

### Control plane and data planes

| Plane | Holds | Location |
|-------|-------|----------|
| Control plane | Users, sessions, 2FA, organizations, workspaces, memberships, roles, invitations, audit log, plans, usage ledger, credentials, AI settings, infrastructure registry | Always the DocuMind database |
| Data plane | Documents, versions, collections, pages, chunks and vectors, processing jobs, conversations, messages, citations | Chosen by the workspace's infrastructure profile |

The **shared data plane** is the DocuMind database itself, so today's deployment is the shared data
plane, and nothing moves.

- **Dedicated data plane:** a database DocuMind provisions for one organization on a DocuMind-managed
  server.
- **External data plane:** a database the customer runs; DocuMind connects with credentials the
  customer provides.

### Profiles and versions

- `infrastructure_profiles`: one per organization workspace. Personal workspaces always use the
  platform defaults.
- `infrastructure_profile_versions`: immutable. Each version has:
  - a version number and a status (`draft` → `validated` → `active` → `retired`, or `failed`);
  - non-secret configuration for `database`, `storage` and `processing`;
  - references to credentials in the vault (never the secrets themselves);
  - validation results;
  - a **SHA-256 checksum** of the canonical configuration, verified whenever the version is loaded, so
    an edited row is detected and refused.
- `workspace_infrastructure` pins a workspace to exactly one active version. A workspace without a row
  uses the platform default (shared database, platform bucket, DocuMind workers).
- Activating a version requires owner rights, a fresh 2FA step-up, and a successful validation of that
  exact version. It is audited, and recorded in `infrastructure_events`.
- If the new version moves the database or storage of a workspace that already holds data, activation
  is refused with `migration_required`. Moving data is the controlled migration workflow (MI-8), which
  needs explicit approval. Processing changes, or credential rotation to the same location, activate
  directly.

### Resolution

`InfrastructureResolver.resolve(workspace_id)` returns a `ResolvedInfrastructure`:

- the version id;
- a data-plane handle (session factory);
- an `ObjectStorageProvider`;
- a processing route.

How it works:

- The workspace → version mapping is cached for a few seconds and invalidated on activation in this
  process. Resolved providers are cached by version id, which is safe because versions are immutable.
- **Provider factories** build adapters from a version's configuration plus decrypted credentials:
  `DataPlaneFactory`, `StorageFactory`, `ProcessingRouteFactory`. Adding a provider means adding a
  factory entry. Nothing else changes.
- Engines for dedicated and external data planes use small pools (default 5) and are evicted LRU
  beyond a process-wide limit.
- Requests get their data-plane session from the resolver, driven only by the authorized
  `WorkspaceContext`. For the shared data plane it is *the request's own session*, so a document change
  and its audit entry still commit together.

### Data-plane schema

- Dedicated and external databases are migrated by a separate Alembic environment
  (`backend/alembic_dataplane`). Its baseline reproduces the shared data tables at revision 0008, except
  that references to `workspaces` point to a local `tenant_workspaces` anchor table and references to
  `users` are plain UUIDs.
- A database therefore accepts rows only for the workspaces registered to it.
- A **parity test** reflects both schemas and fails if a data table differs, so every future change to a
  data table must be made in both environments.
- `audit_outbox` (data plane only) stores audit entries in the same transaction as the change. A relay
  copies them to the control-plane `audit_logs` (idempotent by id) right after commit and on every
  sweep.

### Database modes

- **Dedicated:** `documind-manage dataplane provision <workspace>` connects with
  `DEDICATED_DATABASE_ADMIN_URL`, creates a database and a login role with a random password, stores the
  password in the vault, enables pgvector and runs data-plane migrations.
- **External:** the customer supplies host, port, database, user, password and TLS mode.
  - Validation checks, in order: the host against the SSRF policy; that the connection works; PostgreSQL
    14 or later; that pgvector is available; that the role isn't a superuser; and the schema revision.
    DocuMind runs the data-plane migrations only when the customer ticks "DocuMind may manage the
    schema".
  - In production `sslmode` must be `verify-full`.

### Storage providers (MI-2)

| Provider | Endpoint | Notes |
|----------|----------|-------|
| `documind` | Platform bucket | Default |
| `aws_s3` | AWS regional endpoint | Bucket, region, access key or role |
| `s3_compatible` | HTTPS endpoint, SSRF-checked | Path-style option |
| `minio` | `s3_compatible` with path-style defaults | Development and on-premises |
| `azure_blob` | — | **Planned (MI-6)**: listed but not selectable until an adapter is implemented and tested |

- Validation writes, reads and deletes a probe object under the configured prefix. It also checks that
  the bucket isn't public where the provider exposes that.
- Keys keep the existing layout under an optional prefix.

### Processing (foundations for MI-7)

- Processing modes are `documind`, `customer` or `hybrid`. Customer mode routes jobs to a per-profile
  queue, `processing.customer.<profile>`.
- `worker_registrations` stores customer workers with hashed tokens, scopes, last-seen time and
  revocation.
- The pull-based connector protocol and worker package are MI-7, delivered in Phases 9–10. Until then
  customer mode can be configured but not activated (`not_available`).

### SSRF policy

Any customer-supplied host (database, storage endpoint, later AI endpoints) is resolved and rejected if
it is:

- loopback, private, link-local or carrier-grade NAT;
- the cloud metadata address;
- a multicast or reserved address.

The exception is an operator allowlist (`INFRASTRUCTURE_ALLOWED_PRIVATE_HOSTS`), used for on-premises
deployments. HTTP endpoints must use HTTPS unless an allowlist entry says otherwise. Hosts are
re-checked at connection time to limit DNS rebinding.

## Alternatives considered

- **Schema-per-tenant in the shared database.** Doesn't meet "external" or "dedicated" requirements.
- **Moving identity to each data plane.** Rejected: authorization must stay in the control plane, and
  users belong to many workspaces.
- **Cross-database foreign keys via FDW.** Fragile, and a security risk on customer databases.
- **Mutable profiles.** Rejected: a running job must keep the configuration it started with, and
  changes must be auditable and reversible. Versions give pinning and rollback.

## Consequences

- Data-plane services no longer assume one database; a test suite runs them against two dedicated
  databases to prove isolation.
- Every change to a data table requires migrations in both Alembic environments, enforced by the parity
  test.
- Operational load grows with each dedicated or external database: health checks per profile, backup
  responsibility (DocuMind for dedicated, the customer for external), and connection budgets.
- Moving existing customers between data planes needs the MI-8 migration workflow and their explicit
  approval.
