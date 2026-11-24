# Deployment models

DocuMind separates a **control plane** from **data planes** (ADR 0021):

- the control plane holds identity, membership, authorization, plans, usage, audit and the
  infrastructure registry;
- a data plane holds documents, chunks, embeddings and conversations, and the files live in object
  storage.

Each organization chooses where its data plane, its files and its processing live, using immutable,
validated infrastructure versions. Moving existing data between locations always needs the owner's
explicit approval (ADR 0025).

| Model | Control plane | Database | Files | Processing | Plan |
|-------|---------------|----------|-------|------------|------|
| [DocuMind-managed](#1-documind-managed) | DocuMind | DocuMind shared, or dedicated | DocuMind bucket | DocuMind | any; dedicated needs Business |
| [Hybrid](#2-hybrid) | DocuMind | the customer's, or DocuMind's | the customer's, or DocuMind's | DocuMind, or the customer's workers plus DocuMind's | Pro (own storage), Business (own database), Enterprise (customer workers) |
| [Customer-managed cloud](#3-customer-managed-cloud) | DocuMind | the customer's (their cloud account) | the customer's (S3 or Azure Blob) | the customer's workers only | Enterprise |
| [On-premises](#4-on-premises-self-hosted) | the customer | the customer's | the customer's (MinIO or S3) | the customer's | not applicable (self-hosted) |

Plans are Free, Pro, Business and Enterprise (ADR 0020). Entitlements gate each choice: `custom_storage`, `dedicated_database`,
`external_database` and `customer_workers`.

## 1. DocuMind-managed

This is the default for every new organization. Nothing has to be configured.

- **Shared:** data sits in DocuMind's database and is isolated by `workspace_id` in every query and
  in the foreign keys.
- **Dedicated database:** DocuMind provisions a separate PostgreSQL database and role for the
  organization, on `DEDICATED_DATABASE_HOST`, and keeps it migrated.
  - The owner selects **Dedicated** under **Organization → Infrastructure**, then validates and
    activates it.
  - An organization that already has data gets a migration plan to approve.
- **Backups:** DocuMind backs up everything, including dedicated databases.
- **Operator setup:** `DEDICATED_DATABASE_ADMIN_URL`, `DEDICATED_DATABASE_HOST` and
  `DEDICATED_DATABASE_SSLMODE` ([`CONFIGURATION.md`](CONFIGURATION.md)).

## 2. Hybrid

DocuMind runs the application; the customer keeps some of the data. These are common combinations:

- **Own storage only.** Files go to the customer's S3, S3-compatible, MinIO or Azure Blob storage,
  under a prefix. Text and embeddings stay with DocuMind.
- **Own database and storage, DocuMind processing.**
  - The customer provides PostgreSQL 14 or later with pgvector, plus a non-superuser role.
  - DocuMind's workers read the customer's bucket and write to the customer's database.
  - Outbound connections from DocuMind are checked by the SSRF policy at validation **and** before
    each connection.
- **Hybrid processing.** The customer's workers and DocuMind's both process jobs. This suits
  bursty loads.

**Customer requirements:**

- The database is reachable from DocuMind's egress addresses over TLS. `verify-full` is enforced in
  production; the hostname must match the certificate, using the system CA bundle.
- The bucket or container is private. Public ones are refused.
- Credentials are scoped to that bucket and prefix, or that database.
- Browsers must be able to reach the storage endpoint, because downloads are short-lived signed
  URLs on the customer's storage.

## 3. Customer-managed cloud

Everything that contains document content lives in the customer's cloud account. DocuMind keeps
only control-plane records: users, membership, usage counters and audit.

1. In the customer's account, provision:
   - PostgreSQL with pgvector, for example Amazon RDS, Azure Database for PostgreSQL or Cloud SQL;
   - a private bucket or container;
   - a small VM or container service for workers.
2. In DocuMind, the owner registers the database with **Your own PostgreSQL** and their
   storage, and sets processing to **Your own workers only**.
3. The customer registers a worker and deploys `deploy/customer-worker/`. The worker claims jobs in
   the customer's database. Only heartbeats and usage counts go to DocuMind, over outbound HTTPS.
4. The owner validates (a worker must have checked in within 15 minutes), then activates.

**What still reaches DocuMind:**

- **Answering.** The API process reads relevant chunks from the customer's database to build
  answers, and sends the prompt to the AI provider: DocuMind's key or the organization's own (BYOK).
- **Embeddings.** The customer's worker embeds with the workspace's configured model. Customers who
  need prompts to stay in their own AI account should use BYOK.

**Customer responsibilities:** backups of their database and bucket, patching, worker upgrades in
step with DocuMind releases, and running `alembic -n dataplane upgrade head` if they did not allow
DocuMind to manage the schema.

## 4. On-premises (self-hosted)

The customer runs the whole stack on their own hardware or private cloud: `deploy/production/`,
following [`runbooks/deployment.md`](runbooks/deployment.md).

| Component | On-premises choice |
|-----------|--------------------|
| Database | Local PostgreSQL 16 with pgvector, or the `bundled-db` profile for small sites |
| Object storage | MinIO, or any S3-compatible storage (ADR 0023); `S3_FORCE_PATH_STYLE=true` for MinIO |
| TLS | Let's Encrypt if the host is reachable from the internet. Otherwise mount the internal CA's certificate as `fullchain.pem` and `privkey.pem` in the `letsencrypt` volume (`live/<SERVER_NAME>/`) and leave out the `letsencrypt` profile |
| Email | The internal SMTP relay with STARTTLS |
| AI | OpenAI, or an **OpenAI-compatible** gateway inside the network (`OPENAI_BASE_URL`). The `local` providers are for development only and are refused in production |
| Internal hosts | Organizations pointing at internal databases or storage need `INFRASTRUCTURE_ALLOWED_PRIVATE_HOSTS` listing those exact hostnames. Private addresses are otherwise blocked by the SSRF policy |
| Monitoring | `deploy/monitoring/` (Prometheus and alerts) |
| Backups | The `backup` profile, with an offsite copy to a second site |

**Air-gapped sites** need:

- an OpenAI-compatible model server inside the network;
- images transferred with `docker save` and `docker load`;
- certificates from the internal CA.

Air-gapped operation is supported, but it hasn't been tested end to end with a local model server.

## Changing model later

- Activating a new version with no existing data is immediate.
- With existing data, DocuMind records a migration plan. The owner approves it with 2FA, and the
  data is copied, verified and switched while the workspace is briefly unavailable.
- The old copy is kept until the owner deletes it. See ADR 0025 and
  [`runbooks/incident-response.md`](runbooks/incident-response.md#migration-stuck).
