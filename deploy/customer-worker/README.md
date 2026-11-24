# DocuMind customer-hosted worker (MI-7)

Process your organization's documents inside your own network. The worker reads files from **your**
bucket and writes text, chunks and embeddings to **your** database. Neither the database password nor the
storage keys leave your network.

```
   your network                                         DocuMind
 ┌──────────────────────────────────────┐            ┌───────────────────────┐
 │ documind-worker ──► your PostgreSQL  │  HTTPS     │ control plane         │
 │        │        ──► your S3 bucket   │ ─────────► │  /worker/v1/heartbeat │
 │        └─ embeddings (your key)      │ (outbound) │  /worker/v1/usage     │
 └──────────────────────────────────────┘            └───────────────────────┘
```

## Requirements

- An organization on the Enterprise plan (customer workers are an entitlement).
- In **Organization → Infrastructure**:
  - an **external database** (your PostgreSQL 14 or later, with pgvector);
  - your **storage**;
  - processing set to **Your own workers only**, or **Hybrid** (your workers and DocuMind's).
- Outbound HTTPS from the worker to your DocuMind address. No inbound ports are needed.

## Set up

1. In **Infrastructure → Customer-hosted workers**, register a worker. Copy the token: it is shown once.
2. `cp .env.example .env` and fill it in:
   - the token;
   - the same database and bucket you entered in DocuMind;
   - the embedding model the workspace uses. The heartbeat reports the expected model, and the worker
     refuses to run with another one.
3. Run `docker compose up -d`, then check the logs with `docker compose logs -f worker`.
4. In DocuMind, create the infrastructure version with your processing mode, **Validate**, then
   **Activate**. Validation passes once a registered worker has checked in within the last 15 minutes.

## How it works

- Every `WORKER_POLL_SECONDS`, the worker:
  1. calls the heartbeat endpoint;
  2. claims due jobs in your database atomically, so several workers can run;
  3. downloads and extracts each file (with OCR);
  4. chunks and embeds it, and writes the index;
  5. reports token usage, which is shown on the Usage page as billed to your own key.
- Retries, leases and dead-lettering work exactly as for DocuMind's own workers. DocuMind's sweeper
  recovers jobs whose worker died, but never hands customer-mode jobs to DocuMind's workers.
- **Revocation:** revoking the worker in DocuMind makes its next heartbeat fail with 401, and the worker
  stops with exit code 3.

## Operations

- **Scale:** run `docker compose up -d --scale worker=N`. One worker uses about one CPU while running
  OCR.
- **Upgrade:** pull the new image and restart. Keep the worker on the same release as DocuMind. The
  schema is managed by DocuMind if you allowed it; otherwise run
  `alembic -n dataplane upgrade head` against your database before upgrading.
- **Rotate the token:** register a new worker, deploy its token, then revoke the old one.
- **Exit codes:**
  - 0: stopped;
  - 2: configuration error (see the log);
  - 3: token revoked or unknown.
