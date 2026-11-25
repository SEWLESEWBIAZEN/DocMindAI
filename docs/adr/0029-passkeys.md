# ADR 0029: Passkeys (WebAuthn) as a second factor

- Status: Accepted
- Date: 2026-11-25
- Builds on: ADR 0013, 0026, 0027, 0028 (2FA). Delivers the passkeys item of ADR 0011.

## Context

TOTP codes and email codes can be phished: someone can type them into a look-alike site. Passkeys
can't, because the browser binds each signature to the real origin. Passkeys also suit people who
find typing codes hard: a fingerprint, face or device PIN is enough.

## Decision

### Implementation

WebAuthn verification goes through `py_webauthn` 3.x, kept behind the adapter
`infrastructure/security/passkeys.py`. The application only exchanges the browser's JSON. The web
app converts options and credentials itself (`lib/webauthn.ts`), so no frontend dependency is
needed.

### Relying party

- The RP ID and origin come from `PUBLIC_APP_URL`.
- `WEBAUTHN_RP_ID` and `WEBAUTHN_ORIGINS` override them.
- A response from any other origin is refused, which is what stops phishing.

### What gets stored

**The passkey:** a `webauthn_credentials` row holding only the **public** key, the credential id,
the signature counter, transports, a user-chosen name and whether the passkey is synced.

**Challenges:**
- they are random, single use and valid for 5 minutes;
- they are bound to the user;
- they are stored hashed in `account_tokens` (purposes `webauthn_reg` and `webauthn_auth`).
- The server finds the challenge from the browser's response and accepts it only once, so replayed
  responses fail.
- A signature counter that goes backwards means a cloned authenticator, and the response is
  rejected.

### Where passkeys are accepted

**At a 2FA prompt:**
- sign-in (`/auth/login/mfa/passkey-options`, then `/auth/login/mfa` with `passkey`);
- step-up (`/auth/mfa/passkey-options`, then `/auth/mfa/verify`);
- new recovery codes;
- turning 2FA off.

**Primary method:** a passkey is a full 2FA method. As the first method it turns 2FA on and issues
recovery codes (`users.mfa_passkey_enabled_at`). `User.mfa_methods` lists every method the user can
use, and the sign-in challenge reports them, with passkey first.

**Organizations that refuse email codes (ADR 0028):** passkeys count there, for the requirement and
for step-up. Tokens record the method as `passkey`.

### Managing passkeys

- **Where:** Profile → Passkeys. Up to 10 per user.
- **Adding and removing** both need the password, and each change is emailed and audited.
- **The last method can't be removed:** removing the only 2FA method is refused
  (`last_mfa_method`); turn 2FA off instead.
- **Turning 2FA off** deletes all passkeys.

## Out of scope

- **Passwordless sign-in.** A passkey replaces the second factor, not the password. Usernameless
  passkey sign-in can build on this later.
- **Attestation.** Attestation is `none`: authenticator models aren't verified or restricted.

## Database impact

Migration 0015 is schema only. It adds:
- the table `webauthn_credentials`;
- the nullable column `users.mfa_passkey_enabled_at`;
- two values to the `account_tokens.purpose` check.

## Verification

- `tests/integration/test_passkeys.py` (6 tests). They use a software authenticator with real
  P-256 keys and signatures (`soft_authenticator.py`), so the real verification code runs. They
  cover:
  - set-up as the first factor, and sign-in;
  - replay, and a phishing origin;
  - a cloned authenticator (counter);
  - organizations that refuse email codes;
  - removal rules;
  - registration challenge reuse.
- Playwright `e2e/passkeys.spec.ts` with Chromium's virtual authenticator: add a passkey, sign in
  with it, axe clean.
