# ADR 0040: "Continue with Google" for every visitor

- Status: Accepted
- Date: 2026-11-27
- Builds on: ADR 0026 (2FA at sign-in), ADR 0035 (Google OAuth client), ADR 0036 (organization
  SSO and the OIDC client)

## Context

Signing up needed an email, a password and a verification email. Many people expect a
one-click "Continue with Google", including on personal Gmail addresses. Organization SSO
(ADR 0036) already signs in through Google, but only for an organization's own Google Workspace
domain, set up by its admin.

## Decision

- **Configuration:** one Google OAuth client for the whole server, set by the operator. It's
  the client the Google Drive connector already uses, with a second redirect URI
  `/api/v1/auth/google/callback`. The feature is off until `GOOGLE_SIGNIN_ENABLED=true`, so a
  server set up only for Drive doesn't show a button that would fail.
- **The button:** "Continue with Google" on the sign-in and sign-up pages, shown only when the
  server has it on (`GET /auth/google`).
- **The flow:** the same as organization SSO, with Google as the only provider.
  - Single-use state in Redis, bound to the browser by an HttpOnly nonce cookie, with PKCE.
  - The ID token is verified with Google's keys: issuer, audience, expiry and nonce.
  - The email must be verified by Google.
  - `prompt=select_account` lets people pick the right Google account.
- **Who signs in as whom:**
  - **New email:** a new account and personal workspace, exactly as after a password sign-up
    but already verified. There's no usable password; the person can set one with "Forgot
    password". The audit entry is `user.registered` with `{"via": "google"}`.
  - **Existing account with that email:** that account; their password keeps working too.
    Google says it verified the address, which is the same proof a password-reset email gives.
  - **Address of an organization with active SSO:** sent to that organization's SSO, with the
    email filled in. Google can't be used to get around an organization's identity
    provider.
- **2FA still applies:** the password check and Google now share `continue_sign_in`. People
  with 2FA get the same short-lived challenge and second step as after a password. The
  challenge travels in the URL fragment (`/login#mfa=…`), which browsers don't send to
  servers, and the page removes it from the address bar at once.
- **Where to go afterwards:** the `next` path survives the round trip. Only same-site paths
  are accepted, never `//host` or a full URL.
- **Limits:** starting a sign-in and creating accounts are rate-limited per IP.

## Alternatives

- **A separate OAuth client for sign-in.** Rejected for now: one client is simpler to set up,
  and Google allows several redirect URIs. Splitting is a configuration change later, if Drive's
  restricted-scope review ever needs it.
- **Linking Google accounts by Google's subject id instead of email.** Not needed yet. Email
  matching is how password resets already prove ownership. If a person changes their DocuMind
  email, signing in with Google under the old address creates a new account; a stored link can
  be added if that matters.
- **Other social providers (Microsoft, Apple).** Later. The OIDC client is generic, so each is
  mostly configuration plus a button.

## Consequences

- Sign-up is one click on servers that turn it on.
- Operators must set up a Google OAuth consent screen and add the redirect URI.
- **Not yet run against real Google from the development environment**, which has no internet
  access to Google. Tests use a fake Google that signs real RS256 tokens, so signature,
  issuer, audience, nonce and expiry checks all run.
