# ADR 0026: Two-factor authentication at sign-in, and required 2FA

- Status: Accepted
- Date: 2026-11-25
- Builds on: ADR 0013 (TOTP 2FA and step-up), ADR 0020 (plans), ADR 0011 (accessibility)

## Context

Since ADR 0013, users could turn on TOTP 2FA, but it was only checked before sensitive actions
(step-up). Signing in needed only a password. Organizations also could not require 2FA from their
members. Customers on paid plans expect both.

## Decision

### Sign-in

For a user with 2FA, a correct password does **not** start a session. Instead, `POST /auth/login`
returns `{mfa_required: true, challenge_token, expires_in}`.

**The challenge:**
- is random and stored hashed in `account_tokens` (purpose `mfa_login`);
- is single use;
- lasts `MFA_LOGIN_CHALLENGE_SECONDS` (default 300);
- is replaced whenever the password step is repeated.

**Finishing sign-in:** `POST /auth/login/mfa` exchanges the challenge, plus a TOTP code or a
recovery code, for the normal session (access token and refresh cookie). The code check is the
existing one:
- rate-limited per user;
- replay-safe, because TOTP steps and recovery codes are each used once;
- audited;
- an email is sent when a recovery code is used.

**Access token:** the token issued at this point counts as a fresh 2FA check for the step-up
window, so a sensitive action straight after sign-in doesn't ask for a second code. Refreshed tokens
are never elevated, as before.

**What a wrong password reveals:** nothing changes. It gets the same `invalid_credentials` error, so
it never shows whether an account has 2FA.

### When 2FA is required

A workspace requires 2FA in either of two cases:

- **Its plan is paid** (Pro, Business or Enterprise). This also applies to personal workspaces.
- **Its organization turned it on**, on any plan: `PUT /workspaces/{id}/mfa-requirement`.
  - Admins and owners can change it, and they need a fresh 2FA code to do so.
  - Each change is audited.

Free personal workspaces leave 2FA optional. New accounts are offered it on the dashboard and can
skip the offer.

**Grace period:**
- Members without 2FA have `MFA_ENFORCEMENT_GRACE_DAYS` (default 14). It is counted from
  `workspaces.mfa_enforced_since`, which is set the first time the workspace is used while 2FA is
  required.
- The UI shows a reminder with the deadline. Workspace summaries carry `mfa_required_by` and
  `mfa_enrollment_deadline`.
- After the deadline, every workspace-scoped request returns `403 workspace_requires_mfa`.

**Exceptions:** reading the workspace itself and leaving it always work, and so do the account
pages. Nobody is trapped, and setting up 2FA restores access immediately.

**Where it's enforced:** in `WorkspaceService.resolve_context`, the single entry point every
workspace route already goes through, so no route can be forgotten.

**Lifting the requirement** clears `mfa_enforced_since`. If it is required again later, a new
grace period starts.

**Visibility:** admins see each member's 2FA status in the members list; plain members don't see
other people's status.

## Database impact

Migration 0012 changes the schema only:

- it widens the `account_tokens.purpose` check constraint;
- it adds `workspaces.mfa_required` (default `false`) and `workspaces.mfa_enforced_since`
  (nullable).

No existing row is rewritten. Existing paid workspaces start their grace period on their first use
after the upgrade, so nobody is locked out by the deploy itself.

## Consequences

- **More friction for members of paid workspaces** who don't use 2FA, which is intended.
- **Accessibility:** TOTP is the only factor for now. Passkeys and an email one-time code (the
  accessible fallback named in ADR 0011) remain on the roadmap. Until they exist, a grace period
  gives people time to get set up.
- **Lost authenticator and recovery codes:** a support-assisted reset is still not implemented. An
  operator has to clear 2FA in the database. This is a known gap.
- **Update:** ADR 0027 adds one-time codes by email. They are a fallback for this case, and they
  let users recover a lost authenticator themselves.
- **Extra query:** each workspace request now also reads the workspace's plan, with one primary-key
  lookup.

## Verification

- `tests/integration/test_mfa_sign_in.py` (6 tests):
  - accounts without 2FA;
  - the challenge flow and its single use;
  - recovery codes;
  - expiry and replacement;
  - rate limiting;
  - turning 2FA off.
- `tests/integration/test_mfa_enforcement.py` (5 tests):
  - free workspaces;
  - paid-plan grace, then lockout, then restored access;
  - leaving while locked out;
  - the organization setting with step-up, audit and members visibility;
  - personal paid workspaces and downgrades.
- `tests/unit/test_mfa_policy.py`.
- The Playwright journey signs in again with a code, including a wrong code and an axe check.
