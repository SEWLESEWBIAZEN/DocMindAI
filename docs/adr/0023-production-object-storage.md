# ADR 0023: Production object storage

- Status: Accepted
- Date: 2026-11-24
- Supersedes: the open production decision in ADR 0006 (development use of
  `bitnamilegacy/minio` stays as described there)

## Context

ADR 0006 left production storage open. The upstream MinIO image is no longer distributed, and the frozen
development image receives no security updates. The application only uses the S3 API through boto3:
put, get, head, delete, list and pre-signed GET. Since Phase 8, each organization may also bring its own
S3-compatible storage (ADR 0021).

## Decision

1. **DocuMind-managed deployments use a managed S3 service.**
   - AWS S3 is the reference. S3-compatible managed services (Cloudflare R2, Backblaze B2, Wasabi) are
     supported through `S3_ENDPOINT_URL`.
   - Requirements:
     - a private bucket with Block Public Access (the bootstrap refuses a bucket with a policy);
     - server-side encryption (SSE-S3 or SSE-KMS);
     - versioning on, with a lifecycle rule expiring non-current versions after 30 days;
     - least-privilege credentials limited to the bucket: `s3:PutObject`, `s3:GetObject`,
       `s3:DeleteObject`, `s3:ListBucket`, `s3:GetBucketPolicyStatus`.
2. **No storage server is bundled in the production stack.** The production Compose file has no MinIO
   service, so DocuMind never ships or operates a storage server with no security updates.
3. **On-premises and customer-managed deployments** use the customer's own S3-compatible storage. That
   might be a supported MinIO distribution (AIStor or self-built from source), Ceph RGW or a NetApp/Dell
   object store. It plugs in as the platform storage (`S3_*`) or per organization (ADR 0021, MI-2).
   Compatibility is verified with the storage validation probe (write, read, delete, public check) and the
   restore drill.
4. **Backups** go to a separate offsite bucket, ideally in another account or region. Its credentials can
   write but not delete (`BACKUP_S3_*`, `docs/runbooks/backup-and-restore.md`).

## Consequences

- Development keeps the frozen MinIO image, bound to loopback only (ADR 0006).
- No application change was needed: the decision is configuration and runbook material
  (`deploy/production/.env.example`, `docs/runbooks/deployment.md`).
- Egress and request costs of the managed service apply. Large customers can bring their own bucket
  instead.
