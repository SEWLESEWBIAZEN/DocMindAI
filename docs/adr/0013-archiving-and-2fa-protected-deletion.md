# ADR 0013: Document archiving and 2FA-protected deletion

- Status: Accepted. Amends ADR 0009 (role matrix) and ADR 0012 (deletion).
- Date: 2026-11-23

## Context

Product decision: *Members can archive, not delete, documents they uploaded.
Admins can delete any document, with 2FA verification.* Until now, Members
could permanently delete any document, and there was no second factor.

## Decision

### Permissions (migration `0004`)

| Permission | Owner | Admin | Member | Viewer |
|---|:-:|:-:|:-:|:-:|
| `documents.archive_own`: archive or restore documents you uploaded | ✓ | ✓ | ✓ | |
| `documents.archive_any`: archive or restore any document | ✓ | ✓ | | |
| `documents.delete`: permanent deletion, **with a recent 2FA check** | ✓ | ✓ | ✗ (removed) | |

Ownership means `documents.uploaded_by_user_id`. A document whose uploader
account was deleted can only be archived by an Admin or Owner. Collections are
groupings only (deleting one never deletes documents), so managing them now
needs `documents.write`.

### Archiving

`archived_at` and `archived_by_user_id` on `documents`. Archived documents:

- are hidden from listings unless `archived=only|include` is requested;
- are read-only: rename, new version and adding to a collection return
  409 `document_archived`;
- can still be viewed and downloaded;
- still count for duplicate detection (the error suggests restoring);
- will be excluded from retrieval and answers (Phase 5);
- can be restored by whoever is allowed to archive them.

### Two-factor authentication (TOTP)

- **Method.** RFC 6238 authenticator-app codes (SHA-1, 30 s, 6 digits),
  implemented with the standard library and verified against the RFC test
  vectors. One step of clock drift is allowed either way.
- **Replay protection.** The last used time step is stored, and codes at or
  before it are refused.
- **Enrollment.**
  1. `POST /me/mfa/totp/setup` (requires the account password) returns the
     secret for manual entry and an `otpauth://` URI for a QR code.
  2. `POST /me/mfa/totp/confirm` (requires a valid code) switches 2FA on and
     returns **10 single-use recovery codes**, shown once.
  3. A pending setup expires after 15 minutes.
- **Storage.** Secrets are AES-256-GCM encrypted with `MFA_ENCRYPTION_KEY`,
  with the user ID bound as associated data. Recovery codes are stored as
  HMAC-SHA256 hashes keyed with the same key, so a database dump alone permits
  neither code generation nor offline guessing.
- **Disabling** requires the password **and** a second factor.
- **Rate limits and audit.** Every 2FA endpoint is rate limited per user
  (5 attempts per 5 minutes by default). Failures and successes are audited.
- **Emails.** Users are notified when 2FA is turned on or off, and when a
  recovery code is used.

### Step-up verification

`POST /auth/mfa/verify` takes either a code or a recovery code and returns a
**new access token** with an `mfa` claim holding the verification time.
`DELETE …/documents/{id}` requires:

1. the `documents.delete` permission;
2. 2FA enabled, else 403 `mfa_enrollment_required`;
3. `mfa` within `MFA_STEP_UP_TTL_SECONDS` (default 5 minutes), else 403
   `mfa_step_up_required`.

The checks run *before* the document lookup. Without a step-up every ID gets
the same 403, so the endpoint doesn't reveal whether a document exists.

The refresh cookie is not changed by step-up, so refreshed tokens are never
elevated. Disabling 2FA immediately voids elevation, because the check also
requires 2FA to be enabled. The same rule applies to Owners and inside
personal workspaces: deletion always needs 2FA, archiving never does.

## Alternatives

- *Code in a header on each DELETE:* replay protection would force a new
  code for every deletion in a batch. Step-up allows several deletions within
  5 minutes, which is the standard pattern (OIDC `auth_time` / `amr`).
- *Email or SMS one-time codes:* weaker (account recovery and SIM swap risk)
  and needs a provider. Email OTP may be added later as an accessible
  fallback for users without an authenticator app (ADR 0011).
- *WebAuthn/passkeys:* strongest option, planned as a future second method.
  It needs frontend work (Phase 7).
- *`pyotp` dependency:* unnecessary for about 40 lines of well-specified
  code with official test vectors.

## Consequences

- 2FA is currently required **only for step-up** (deletion), not at sign-in.
  Requiring it at login for enrolled users, and optionally enforcing
  enrollment per workspace, are recommended next steps.
- Losing both the authenticator and the recovery codes means an Admin can't
  delete until 2FA is reset. There is no support-assisted reset flow yet.
- `MFA_ENCRYPTION_KEY` is a new required secret. Rotating it needs a
  re-encryption migration; ciphertexts carry a `v1:` version prefix to allow
  that.
