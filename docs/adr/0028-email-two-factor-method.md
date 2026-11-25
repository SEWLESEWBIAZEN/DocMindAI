# ADR 0028: Email as a 2FA method, and organizations that allow or refuse it

- Status: Accepted
- Date: 2026-11-25
- Builds on: ADR 0026 (2FA at sign-in, required 2FA), ADR 0027 (email codes as a fallback)

## Context

Since ADR 0027, email codes have been a fallback for people who already have an authenticator app.
That still left two gaps:

- People who can't use an authenticator app at all could not turn 2FA on. That made them unable to
  meet a paid plan's 2FA requirement. Not having a smartphone, or using assistive technology that
  struggles with authenticator apps, are typical reasons (ADR 0011).
- Regenerating recovery codes still needed the authenticator app.

Some organizations, on the other hand, don't want a mailbox to count as a second factor.

## Decision

### Email-only 2FA

Users can turn 2FA on with codes by email instead of an app:

1. `POST /me/mfa/email/setup` takes the password, then a code is emailed.
2. `POST /me/mfa/email/confirm` takes that code and returns **recovery codes**, which still matter
   if the mailbox is lost.

`users.mfa_email_enabled_at` records it. `User.mfa_method` is `"authenticator"`, `"email"`, or
`None` when 2FA is off.

**Sign-in:** the challenge lists the user's `methods`. For email-only users it is
`["email", "recovery_code"]`, and the web app sends the code at once.

**Switching:** an email-only user can add an authenticator later, which then becomes the primary
method; email codes stay available as the ADR 0027 fallback. Turning 2FA off removes both methods.

**Unchanged from ADR 0027:** every email code is single use, short-lived, rate-limited, stored as an
HMAC and announced by a notice. Email codes are also refused for 24 hours after a password reset.
For an email-only user that means a recovery code is the only way in during that window, which is
intended: the mailbox alone must not be enough.

### Regenerating recovery codes

`POST /me/mfa/recovery-codes` accepts an authenticator code **or** an email code. A recovery code
can't be used to mint new ones.

### Organization setting

`workspaces.allow_email_mfa` defaults to true, which keeps today's behaviour.
- **Who changes it:** organization admins, with a fresh 2FA check. The endpoint is
  `PUT /workspaces/{id}/mfa-requirement` with `allow_email_codes`, and every change is audited.
- **What happens when it's off:**
  - In that workspace only an authenticator counts. It counts toward the 2FA requirement (members
    with email-only 2FA get the grace period, then `403 workspace_requires_mfa`) and toward step-up.
  - Access tokens now record **how** 2FA was verified (claim `mfm`), so a step-up done with an email
    code is not accepted there.
  - Admins see each member's method in the members list.
- **Where it applies:** sign-in itself is account-wide and not tied to a workspace, so email codes
  still sign people in. They just don't satisfy the strict workspace.

## Database impact

Migration 0014 changes the schema only: it adds two columns, `users.mfa_email_enabled_at` (nullable)
and `workspaces.allow_email_mfa` (default `true`). No rows are rewritten.

## Consequences

- **Weaker than an authenticator:** email-only 2FA depends on the security of the user's mailbox.
  The UI says so and recommends an app. Organizations that need more can turn email codes off.
- **Strict organizations:** members who use email codes must add an authenticator app to keep
  using them after the grace period. Their other workspaces are unaffected.

## Verification

- `tests/integration/test_mfa_email_method.py` (4 tests):
  - email-only set-up, sign-in methods, and switching to an app;
  - recovery codes from an email code;
  - a strict organization: setting, step-up by email refused, grace then lockout, members view,
    audit;
  - turning email 2FA off.
- Playwright `e2e/mfa-email.spec.ts`: set-up with a code from the inbox, then sign-in with an
  automatically sent code; axe clean.
