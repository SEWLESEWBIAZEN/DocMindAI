# ADR 0035: Connectors, starting with Google Drive

- Status: Accepted
- Date: 2026-11-26
- Builds on: ADR 0019/0022 (credential vault), ADR 0021 (data planes), ADR 0013 (2FA step-up)

## Context

Uploading by hand is the main friction for teams: documents live in shared drives and change
there. Answers are only as current as the last upload.

## Decision

### Connecting

- **Who:** admins and owners (`workspace.update`), with a recent 2FA check, the same as other
  credential changes.
- **Flow:** OAuth 2.0 authorization code with PKCE, `access_type=offline`, scope `drive.readonly`.
  The refresh token is stored in the credential vault (kind `connector_token`) and is never
  returned by the API.
- **Callback protection:** the callback is bound to the browser that started the flow. The `state`
  is single-use and kept in Redis with the PKCE verifier for ten minutes. An HttpOnly, SameSite=Lax
  nonce cookie, scoped to the callback path, must match. Without that binding, someone could send
  a victim their own authorization link and connect the victim's Drive to the sender's workspace.
- **Choosing folders:** admins choose up to 20 folders, from My Drive or shared drives,
  subfolders included. Files go into a collection, "Google Drive" by default.

### Syncing (worker)

- **First sync:** walk the folders and import supported files (PDF, Word, text, Markdown, and
  Google Docs exported as .docx), then store Drive's change cursor.
- **Later syncs:** read only the change feed.
  - A new or changed file becomes a new document or a new version.
  - A file that is removed, trashed or moved out is **archived**, never deleted. It is restored if
    it comes back.
- **The upload pipeline applies:** imports run as the person who connected the drive and go
  through scanning, processing, plan quotas and audit. One bad file is skipped and the sync
  continues.
- **Pausing:** a quota stops the sync and pauses the connection, and the next sync continues. At
  most `CONNECTOR_MAX_FILES_PER_SYNC` downloads happen per run.
- **Scheduling:** every `CONNECTOR_SYNC_INTERVAL_SECONDS`, plus "Sync now". A Redis lock stops two
  syncs of one connection overlapping.
- **Revoked access** (`invalid_grant`) marks the connection to be connected again. If the person
  who connected it leaves the workspace, the connection stops.
- **Disconnecting:** needs 2FA. The token is revoked at Google and destroyed in the vault. The
  file mapping is removed, and the documents stay.

### Storage

- `connector_connections` is control plane: account, folders, cursor and the vault reference.
- `connector_items`, which maps each Drive file to a document, names files, so it lives in the
  workspace's data plane, including customer databases.

## Consequences

- **Permissions:** Drive sharing permissions are not mirrored. Everything in the chosen folders is
  visible to the workspace, or, with per-collection access (ADR 0036), to the members of its
  collection.
- **Google verification:** `drive.readonly` is a restricted scope, so a public deployment needs
  Google's app verification. Internal apps don't.
- **Not yet run against live Google:** the adapter follows the stable Drive v3 and OAuth
  endpoints, but Google's documentation and APIs aren't reachable from the build sandbox. It is
  tested with mocked HTTP and a fake drive. Test it with a real OAuth client before rollout.

## Database impact

Schema only:
- migration 0019 adds `connector_connections` and `connector_items`, and allows the credential
  kind `connector_token`;
- dp0003 adds `connector_items` to data planes.

## Verification

- `tests/unit/test_google_drive.py` (4 tests) covers requests and responses with HTTP mocked.
- `tests/integration/test_connectors_api.py` (3 tests) covers:
  - connecting, choosing folders, the first sync, edits, new subfolders, trash, moving out and
    back;
  - browser binding and single-use state;
  - 2FA and roles;
  - quota pause and resume, revoked access, disconnecting;
  - the not-configured case.
- `e2e/connectors.spec.ts` checks the page with axe.
