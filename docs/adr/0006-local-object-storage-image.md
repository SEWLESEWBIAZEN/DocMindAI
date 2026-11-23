# ADR 0006: Local object storage image after MinIO's distribution change

- Status: Accepted for development. **Open for production. Decision required before Phase 10.**
- Date: 2026-11-23

## Context

The specification calls for S3-compatible storage with MinIO locally. During
Phase 1 we found that the upstream `minio/minio` image **is no longer
available on Docker Hub**: pulls fail with "repository does not exist". This
follows MinIO's 2025 change to stop publishing free community binaries and
images. `quay.io/minio/minio` could not be checked from the build environment.

The application talks to storage only through boto3 and the S3 API. Business
logic does not depend on which server provides it.

## Decision (development)

- Use **`bitnamilegacy/minio:2025.7.23`** as the default development image. It
  is a frozen, no-longer-updated MinIO build from Bitnami's legacy namespace.
  It is available and works with our S3 usage (verified by integration tests).
- The image is overridable via `MINIO_IMAGE`. The Compose service uses the
  Bitnami layout (`/bitnami/minio/data`). Switching to an image with a
  different layout means also changing the volume path (and the command, for
  the upstream image: `server /data --console-address :9001`).

## Open decision (production)

A frozen image must not be used in production. In particular, MinIO builds
before `RELEASE.2025-10-15` are affected by published security advisories. Options:

1. **Managed S3** (AWS S3, Cloudflare R2, Backblaze B2, Wasabi): no storage to
   operate, but adds a cloud dependency and egress costs.
2. **MinIO built from source** (AGPLv3) into our own image: we track releases ourselves.
3. **Alternative S3-compatible server** (e.g. Garage, SeaweedFS): actively
   distributed, but a different operational profile. Compatibility must be
   verified for pre-signed URLs and multipart uploads.
4. **Commercial MinIO (AIStor)** subscription.

Because the code depends only on the S3 API, any option is a configuration
and deployment change. No application change is needed.

## Consequences

- Development works today. The image receives no security updates, which is
  acceptable only for local, loopback-bound development.
- Phase 10 must close this ADR with a superseding decision.
