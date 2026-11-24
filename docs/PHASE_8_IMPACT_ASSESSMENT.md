# Phase 8 impact assessment: SaaS readiness and infrastructure independence

- Date: 2026-11-24
- Scope: original Phase 8 (usage, quotas, plans, entitlements, BYOK) plus the approved
  multi-infrastructure workstreams MI-1 to MI-5, with foundations for MI-6 to MI-8.
- Inputs: `docs/IMPLEMENTATION_STATUS.md`, ADRs 0001–0019, and the code on branch
  `ccr-ac2d5c32-hw6qxt` at commit `6f83948`, inspected directly rather than taken from earlier reports.

## 1. What exists today (verified in code)

| Area | Current implementation | Evidence |
|------|------------------------|----------|
| Database | One PostgreSQL database for everything, one async engine (`infrastructure/database/engine.py`), one session per request (`api/dependencies.get_session`) | `container.py` |
| Tenancy | `workspace_id` on every data row; composite tenant foreign keys; `WorkspaceContext` authorization (non-members get 404) | ADR 0009, `models/*.py` |
| Object storage | One platform bucket, one boto3 client, keys `workspaces/{ws}/documents/{doc}/versions/{v}` | `storage/s3_provider.py`, `document_service._key` |
| Processing | Celery task `process_document(job_id)`; the sweeper scans one database | `worker/tasks.py`, `processing/sweeper.py` |
| AI providers | One platform key (`OPENAI_API_KEY`) built once in the container for embeddings, chat, vision, STT and TTS | `container._build_*` |
| Secrets | `SecretBox` (AES-256-GCM, context-bound) for TOTP secrets, keyed by `MFA_ENCRYPTION_KEY` | `security/secret_box.py` |
| Usage | Computed on the fly from operational tables (documents, job stats, message tokens); no ledger, no plans, no quotas | `application/usage/summary.py` |
| Audit | `audit_logs` written in the same transaction as the change (`AuditRecorder`) | `identity/common.py` |
| 2FA step-up | `ensure_recent_mfa` guards permanent deletion | `domain/identity/policies.py` |

Missing entirely: plans, entitlements, a usage ledger, quotas, audio-minute and vision metering, BYOK,
infrastructure profiles, per-workspace storage or databases, and customer-hosted processing.

## 2. Architectural impact

### 2.1 Control plane and data plane (major change, ADR 0021)

Dedicated and external databases need the tables split into two groups:

- **Control plane** (always the DocuMind database): users, sessions, tokens, 2FA, organizations,
  workspaces, memberships, roles, invitations, audit log, plans, subscriptions, usage ledger, credentials,
  AI provider settings, and the infrastructure registry.
- **Data plane** (where the workspace's profile says): documents, versions, collections, pages, chunks
  and embeddings, processing jobs, conversations, messages and citations.

Impact on the code:

1. Data-plane services get their session from a **data-plane resolver** instead of the request session.
   For the shared data plane the resolver returns *the same session*, so existing behaviour and
   transactional guarantees are unchanged.
2. Data-plane tables reference `workspaces` and `users`, which live in another database for dedicated or
   external data planes. The data-plane schema replaces those foreign keys with a local `tenant_workspaces`
   anchor table, so a data-plane database accepts rows only for the workspaces registered to it. User
   references become plain UUIDs.
3. One cross-plane join exists: the usage summary joins `conversations` with `users`. It is split into two
   queries.
4. Audit entries for data-plane changes in a dedicated database are written to a **transactional outbox**
   in that database and relayed to the control-plane `audit_logs` (idempotent by id), so an audit entry is
   never lost and never recorded for a change that rolled back.
5. Background work carries the workspace id (`process_document(job_id, workspace_id)`); the sweepers
   iterate every active data plane.

### 2.2 Database model changes

All new tables are additive. **No existing column is dropped or changed and no existing data is moved.**
Every existing workspace keeps using the shared database and the platform bucket, its implicit default
profile.

| Migration | Tables | Plane |
|-----------|--------|-------|
| 0009 plans and usage | `plans`, `workspace_plans`, `usage_records` | control |
| 0010 credentials and BYOK | `credentials`, `workspace_ai_settings`, `workspace_ai_providers` | control |
| 0011 infrastructure registry | `infrastructure_profiles`, `infrastructure_profile_versions`, `workspace_infrastructure`, `infrastructure_events`, `worker_registrations`, `infrastructure_migrations` | control |
| Data-plane baseline (new Alembic environment `alembic_dataplane`) | the data-plane tables, `tenant_workspaces`, `audit_outbox` | dedicated and external databases only |

The data-plane baseline mirrors the shared schema at revision 0008. A parity test compares the two schemas
by reflection, so a future change to a data table that is applied to only one of them fails CI.

### 2.3 Behaviour changes visible to users

- Quotas can now reject uploads, questions and server speech with `402 quota_exceeded`. The default
  "Free" plan limits are generous enough that the existing journeys and tests are unaffected.
- Usage pages show ledger-based figures, including audio minutes and figure descriptions, split by who
  pays (platform or the workspace's own key).
- New pages: Organization → AI providers (BYOK) and Organization → Infrastructure, plus plan details on
  the Usage page.

## 3. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Cross-tenant data access through a wrong data plane | Resolution comes only from `WorkspaceContext.workspace_id`; `tenant_workspaces` FKs reject rows for unregistered workspaces; isolation tests run with two dedicated databases |
| Secret leakage (keys, database passwords) | Envelope encryption with a key ring (ADR 0022); write-only APIs; secrets redacted from errors and logs; tests assert no secret appears in responses or logs |
| SSRF through customer endpoints (storage, database, AI) | Host validation: HTTPS only for HTTP endpoints, DNS resolution checked against private, loopback, link-local and metadata ranges, unless an operator allowlist permits it |
| Silent fallback to the platform key | A workspace with a configured key never falls back; "use my key only" also blocks fallback when no key exists |
| Embedding inconsistency after a provider or model change | Retrieval already filters by `embedding_model`; the change is refused unless a re-index is started in the same request |
| Profile tampering | Versions are immutable and carry a SHA-256 checksum of their canonical configuration, verified on every resolution |
| Migration safety | Switching the database or storage location of a non-empty workspace is refused with `migration_required`. The migration workflow is MI-8, and no customer data is moved without explicit approval |
| Connection-pool exhaustion with many data planes | Small per-data-plane pools, LRU eviction of idle engines, an upper bound on open data planes per process |

## 4. Scope for Phase 8

Implemented in Phase 8:

1. Plans, entitlements, the usage ledger, quotas, and audio-minute and figure-description metering.
2. The credential vault and BYOK (OpenAI): 2FA-protected changes, validation, per-capability resolution,
   no silent fallback, `billed_to` attribution, and the re-index flow.
3. MI-1: infrastructure registry, versioned profiles, resolver and provider factories.
4. MI-2: storage providers (DocuMind-managed, AWS S3, S3-compatible, MinIO) per workspace.
5. MI-3 and MI-4: dedicated (DocuMind-provisioned) and external (customer) PostgreSQL data planes.
6. MI-5: organization infrastructure settings UI.

Foundations only, completed in later phases:

- **MI-6 (Azure Blob):** the provider is registered as `planned` and can't be selected. No adapter is
  shipped until it is implemented and tested.
- **MI-7 (customer-hosted workers):** a processing mode on profiles, a worker registration model with
  hashed tokens and revocation, and queue routing.
- **MI-8:** health events per profile, and the `infrastructure_migrations` state machine (plan only, no
  data movement).

Not in scope: payment processing (the billing model is documented, and plans are assigned by operators
through `documind-manage`).
