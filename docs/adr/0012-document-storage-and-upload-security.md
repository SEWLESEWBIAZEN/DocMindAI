# ADR 0012: Document storage and upload security

- Status: Accepted
- Date: 2026-11-23

## Context

Phase 3 introduces untrusted file uploads (PDF, DOCX, TXT, Markdown) stored in
S3-compatible storage. Requirements: MIME and signature validation, size
limits, internal object keys, private buckets, short-lived signed URLs, no
storage credentials in the frontend, path-traversal prevention, malware
scanning where feasible, and strict workspace isolation.

## Decision

**Uploads go through the API, not directly to storage.**

1. The body-size middleware rejects oversized requests with 413: up front via
   `Content-Length`, and mid-stream for chunked bodies. The cap is
   `MAX_UPLOAD_BYTES` plus 1 MiB for multipart framing.
2. In a worker thread, the service checks the size, sanitizes the filename,
   validates the file and computes SHA-256.
3. The `MalwareScanner` port scans the file.
4. Duplicate content (same SHA-256) in the same workspace is rejected.
5. The file is written to storage under a generated key, then the database
   rows are committed. If the commit fails, the object is deleted again.

Validation never trusts the client:

- The extension must be on the allowlist **and** agree with the content.
  - PDF: header present.
  - DOCX: a ZIP containing `[Content_Types].xml` and `word/document.xml`
    with the WordprocessingML main content type. Macro-enabled documents
    (`vbaProject.bin` or a `macroEnabled` content type) are rejected.
  - TXT and Markdown: strict UTF-8 with no NUL bytes.
- The client's `Content-Type` is ignored. The media type we store is derived
  from the validated format.

**Object keys are generated:**
`workspaces/{workspace_id}/documents/{document_id}/versions/{version_id}`. User
filenames are sanitized (path components, control and bidi characters, leading
dots removed) and used **only** for display and `Content-Disposition`.

**Downloads use signed URLs.**

- `GET …/download-url` authorizes the caller, audits the request, and returns
  a GET URL valid for `SIGNED_URL_TTL_SECONDS` (default 5 minutes).
- The URL forces `Content-Disposition: attachment` (RFC 6266/5987, header-safe)
  and a media type we chose. Stored files are never rendered as active content
  on the storage origin.
- URLs are signed for `S3_PUBLIC_ENDPOINT_URL` (browser-reachable) with a
  separate presign-only client, while the API talks to storage internally.
  No credentials reach the frontend, and the bucket stays private
  (verified: unsigned, tampered and expired URLs return 403).

**Malware scanning.** `MalwareScanner` port with two implementations:

- `none` (default): versions are recorded with `scan_status = not_scanned`.
- `clamav`: clamd `INSTREAM` protocol over TCP, no extra dependency.

If ClamAV is configured but unavailable, uploads **fail closed** with 503.

**Data model.**

- `documents` describes the current version and carries the processing status
  (`pending → processing → ready | failed`). Phase 4 picks up `pending`
  documents, so the status row itself is the durable work record: a failed
  job dispatch can't lose an upload.
- `document_versions` rows are immutable.
- Versions and collection items reference their parents through composite
  `(id, workspace_id)` foreign keys. The database itself therefore rejects any
  cross-workspace link (verified by test).
- A unique `(workspace_id, checksum_sha256)` index prevents duplicates.
  Collection names are unique per workspace, case-insensitively.

**Deletion.** Rows are deleted and committed first, then objects are removed.
A failed object delete leaves a logged orphan in a private bucket, never a
document pointing at a missing file.

## Alternatives

- *Browser uploads straight to storage via presigned PUT:* less API bandwidth,
  but unvalidated bytes would land in the bucket first and storage would have
  to be reachable for writes from browsers. We may revisit this for very large
  files, using a quarantine prefix and post-upload validation.
- *Proxying downloads through the API:* simpler networking, but it ties up API
  workers on large transfers, and the specification asks for signed URLs.
- *libmagic for type detection:* a native dependency for a check our four
  formats can do precisely with the standard library.
- *ClamAV enabled by default:* clamd needs about 1–1.5 GB of RAM, a big share
  of the 8 GB target. Left opt-in and documented.

## Consequences

- Upload size is bounded by memory and disk spool (default 50 MB). Production
  Nginx must set a matching `client_max_body_size` (Phase 10).
- Duplicate detection is per workspace and by exact bytes.
- Members (not only admins) may delete documents in organization workspaces.
  Restricting deletion to the uploader or admins is a possible refinement.
- Orphaned objects need a periodic sweeper (Phase 4 or 10).
