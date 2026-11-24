# Architecture decision records

Each ADR captures one significant decision: its context, the decision, the
alternatives considered and the consequences. ADRs are immutable once
accepted. To change a decision, add a new ADR that supersedes the old one.

| ADR | Title | Status |
| --- | ----- | ------ |
| [0001](0001-monorepo-and-layered-backend.md) | Monorepo and layered (ports-and-adapters) backend | Accepted |
| [0002](0002-python-toolchain.md) | Python toolchain: Python 3.12, uv, pyproject + uv.lock | Accepted |
| [0003](0003-async-data-access-and-migrations.md) | Async SQLAlchemy + asyncpg, Alembic, pgvector in the primary database | Accepted |
| [0004](0004-configuration-and-secrets.md) | Environment-based configuration with fail-fast validation | Accepted |
| [0005](0005-logging-and-health-checks.md) | Structured stdlib logging, request IDs, liveness vs readiness | Accepted |
| [0006](0006-local-object-storage-image.md) | Local object storage image after MinIO's distribution change | Accepted (development); production: 0023 |
| [0007](0007-frontend-toolchain.md) | Frontend toolchain: Vite, TypeScript 7, oxlint, Vitest | Accepted |
| [0008](0008-authentication-and-sessions.md) | Authentication and session tokens | Accepted |
| [0009](0009-tenancy-and-authorization.md) | Tenancy model and authorization policy | Accepted |
| [0010](0010-application-layer-persistence.md) | Application services use concrete repositories (amends 0001) | Accepted |
| [0011](0011-voice-and-accessibility.md) | Voice assistant and accessibility for blind and low-vision users | Accepted (roadmap) |
| [0012](0012-document-storage-and-upload-security.md) | Document storage and upload security | Accepted |
| [0013](0013-archiving-and-2fa-protected-deletion.md) | Document archiving and 2FA-protected deletion (amends 0009, 0012) | Accepted |
| [0014](0014-document-processing-pipeline.md) | Document processing pipeline | Accepted |
| [0015](0015-retrieval-and-grounded-answers.md) | Retrieval and grounded answers (RAG) | Accepted |
| [0016](0016-conversations-and-streaming.md) | Conversations and answer streaming (amends 0015) | Accepted |
| [0017](0017-frontend-application-architecture.md) | Frontend application architecture | Accepted |
| [0018](0018-voice-and-accessible-reading.md) | Voice and accessible reading (implements 0011) | Accepted |
| [0019](0019-bring-your-own-api-key.md) | Bring your own API key (BYOK) | Accepted (amended by 0022) |
| [0020](0020-plans-usage-and-quotas.md) | Plans, entitlements, usage metering and quotas | Accepted |
| [0021](0021-infrastructure-profiles-and-data-planes.md) | Infrastructure profiles, resolution and data planes | Accepted |
| [0022](0022-credential-vault.md) | Credential vault (envelope encryption and rotation) | Accepted |
| [0023](0023-production-object-storage.md) | Production object storage (closes 0006) | Accepted |
| [0024](0024-customer-hosted-processing.md) | Customer-hosted processing (MI-7) | Accepted |

Template: Context, Decision, Alternatives, Consequences.
