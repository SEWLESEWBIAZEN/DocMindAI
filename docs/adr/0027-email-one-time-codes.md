# ADR 0027: One-time codes by email, and recovering a lost authenticator

- Status: Accepted
- Date: 2026-11-25
- Builds on: ADR 0013 (TOTP and recovery codes), ADR 0026 (2FA at sign-in), ADR 0011 (an accessible
  fallback factor)

## Context

**Before this change**, a user with 2FA who didn't have their authenticator app at hand could only
use a saved recovery code:
- if the phone was lost or broken;
- or if the app wasn't usable for them.

**The gap:** without a recovery code, nothing worked short of an operator editing the database.
ADR 0011 also asked for an accessible fallback to app-generated codes.

## Decision

### Email codes as a second factor

A user who has 2FA can ask for a 6-digit code by email instead of using the app. The code is
accepted in three places:

- **when signing in:** `POST /auth/login/mfa/email-code` with the sign-in challenge, then
  `POST /auth/login/mfa` with `email_code`;
- **at step-up, before sensitive actions:** `POST /auth/mfa/email-code`, then
  `POST /auth/mfa/verify` with `email_code`;
- **when turning 2FA off:** `POST /me/mfa/disable` with the password and `email_code`.

Recovering a lost authenticator therefore takes three steps:
1. sign in with an email code;
2. turn 2FA off with the password and a second email code;
3. set 2FA up again on the new phone.

Profile → Security has a "Lost your authenticator app?" button for this.

**Code properties:**
- a code is valid for `MFA_EMAIL_CODE_MINUTES` (10), works once, and only the latest code is
  valid;
- it is generated with `secrets`;
- it is stored only as an HMAC (keyed with the 2FA pepper and the row id), never in plain text;
- it goes through the same per-user attempt limit and audit as TOTP codes;
- sending is limited separately: `RATE_LIMIT_MFA_EMAIL_PER_USER`, 3 per 15 minutes.

**Not covered:** regenerating recovery codes still needs the authenticator. ADR 0028 changes this:
an email code now works too.

### Safeguards

Email codes make the mailbox a second factor. A password reset also only proves control of the
mailbox, so on their own the two would let someone holding the mailbox take over a 2FA account.
Therefore:

- **Refused after a password reset:** email codes are refused for **24 hours after a password reset
  by email** (`email_code_unavailable`). The authenticator and recovery codes still work in that
  window.
- **Security notice:** every successful use of an email code emails the account: "An email code
  was used on your account".
- **Email warns:** the code email tells the user to change their password if they didn't ask for
  it.
- **Audit:** the method (`email_code`) is recorded in `auth.mfa_verified` and `user.mfa_disabled`.

Email codes are a fallback for people who already have 2FA, not a way to enrol. "2FA on" still
means an authenticator is registered. Email-only 2FA, for people who can't use an authenticator app
at all, is a possible next step, together with passkeys. ADR 0028 adds email-only 2FA and a
per-organization switch.

## Database impact

Migration 0013 changes the schema only: it adds `mfa_email` to the `account_tokens.purpose` check
constraint. No rows change.

## Consequences

- An account whose mailbox is compromised, **and** whose password is also known, is no longer
  protected by 2FA alone; the reset safeguard only closes the "mailbox only" case. That is the
  usual trade-off of email fallback. The security notices make the compromise visible.
  Organizations with stricter needs could later be allowed to turn email codes off. This is now
  implemented in ADR 0028.
- Delivery depends on SMTP. When email is down, users fall back to the app or recovery codes.

## Verification

- `tests/integration/test_mfa_email_codes.py` (6 tests):
  - sign-in with an email code, single use, latest code only, hashed at rest;
  - expiry;
  - the full lost-authenticator recovery;
  - step-up;
  - refusal after a password reset;
  - send limits, and that the user must have 2FA.
- The Playwright journey signs in with a code read from the dev mailbox (Mailpit).
