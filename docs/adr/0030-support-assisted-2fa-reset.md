# ADR 0030: Support-assisted 2FA reset

- Status: Accepted
- Date: 2026-11-25
- Builds on: ADR 0026 to 0029 (2FA, email codes, passkeys)

## Context

Someone can lose every second factor at once: their phone, the passkeys on it, their recovery
codes and access to their email, for example after leaving an employer whose mailbox they used. The
only remaining path was an operator editing the database by hand: unaudited, with no delay, and
easy to talk someone into. Account-recovery requests are a classic target of social engineering,
so the process has to resist an attacker who knows the password and tells a convincing story.

## Decision

### Request

At the 2FA step of sign-in, which already proves the password, the user can choose "Lost access to
all of these?" and describe what happened:

- `POST /auth/login/mfa/reset-request` takes the challenge and a reason of 10 to 1000 characters;
- the request is rate-limited per address;
- one request can be open per account at a time.

The request is stored in `mfa_reset_requests` with the requester's IP address and user agent, and
audited. The account's email gets a notice, in case the real owner still reads it.

### Review by support, outside the app

Support verifies identity through a separate channel: a video call with ID, a contact at the
organization, or similar. Then:

```bash
documind-manage mfa-reset list --status pending
documind-manage mfa-reset approve <id> --operator <name> --note "how identity was verified"
documind-manage mfa-reset deny <id> --operator <name> --note "..."
```

- Operator names and notes are required and recorded.
- Decisions are final.
- The user is emailed in both cases.

### Waiting period

An approved reset takes effect only after `MFA_RESET_DELAY_HOURS` (72 by default). The approval
email says when.

- **Cancellation:** if anyone verifies a real second factor during the waiting period, every open
  request for the account is cancelled automatically and the account is emailed. Real second
  factors are a passkey, the app, a recovery code or an email code, at sign-in or step-up.
  Cancellation protects an owner who still has their factors from a reset requested by someone
  else.
- **When it takes effect:** at the next sign-in with the password after `effective_at`, all
  factors are removed: app, passkeys, email 2FA and recovery codes. The user signs in, is told to
  set 2FA up again, and workspaces that require 2FA give the usual grace period. Everything is
  audited (`user.mfa_reset_*`) and emailed.

## Consequences

- **Wait:** a legitimate user waits about three days. That is the price of making social
  engineering slow and visible.
- **Operator process:** the safety of identity verification depends on support's process. The
  runbook says what to check, and the operator and method are recorded with each decision.
- **No admin UI:** there is no web screen for support; review is done with the CLI on purpose, for
  now.

## Database impact

Migration 0015 is schema only. It adds the table `mfa_reset_requests`.

## Verification

`tests/integration/test_mfa_support_reset.py` (4 tests) covers:
- request, approval, the waiting period, then the reset at sign-in, with factors and recovery codes
  removed and the notices sent;
- automatic cancellation when the owner signs in with their app;
- denial;
- validation: a reason is needed, and so is a real password challenge.

The Playwright passkeys spec sends a reset request from the sign-in screen.
