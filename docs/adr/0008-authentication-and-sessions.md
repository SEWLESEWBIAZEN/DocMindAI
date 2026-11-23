# ADR 0008: Authentication and session tokens

- Status: Accepted
- Date: 2026-11-23

## Context

DocuMind is a browser SPA backed by a JSON API. Phase 2 requires
registration, login, logout, Argon2id hashing, access tokens, refresh-token
rotation and revocation, password reset, email verification, account
enumeration protection and rate limiting.

## Decision

**Passwords.** Argon2id via `argon2-cffi`, with RFC 9106 parameters by default
(t=3, m=64 MiB, p=4). The parameters are configurable, and hashes are upgraded
on login when the parameters change. Hashing runs in a worker thread.
Passwords must be 10–128 characters and must not equal the email address.

**Access tokens.** HS256 JWT, valid for 15 minutes. All of `iss`, `aud`, `sub`,
`iat`, `nbf`, `exp`, `typ` and `ver` are required, and only HS256 is accepted.
The SPA keeps access tokens in memory and sends them as `Authorization: Bearer`.
`ver` must equal `users.auth_version`. Bumping that version on a password
change, password reset or "sign out everywhere" invalidates every outstanding
access token immediately.

**Refresh tokens.** Opaque tokens of 256 random bits. Only their SHA-256 hash
is stored. They travel in a cookie with these attributes:
`HttpOnly; Secure; SameSite=Strict; Path=/api/v1/auth`. The cookie therefore
can't be read by JavaScript and is never sent to other endpoints.

- **Rotation on every use.** Each token belongs to a *family* (one sign-in).
- **Reuse detection.** Presenting a token that was already rotated revokes the
  whole family and writes an audit event. A 10-second grace window tolerates
  two tabs refreshing at the same moment: the second request gets 401 without
  revoking the family.
- **Lifetime.** 14 days, sliding with each rotation.
- **Logout** revokes the family. **Sign out everywhere** revokes all of the
  user's families and bumps `auth_version`.

**CSRF.** Only `/auth/refresh` and `/auth/logout` are authenticated by cookie.
They require the custom header `X-CSRF-Protection: 1`. Browsers can't add that
header to a cross-site request without a CORS preflight, and the API never
approves that header. This sits on top of `SameSite=Strict`. All other
endpoints use bearer tokens, which browsers never attach automatically.

**Email verification.** Sign-in is refused until the email is verified. The
"not verified" response is revealed only after a correct password, so it can't
be used to enumerate accounts. Verification and reset tokens are single-use,
stored as hashes, and expire after 24 hours and 30 minutes respectively. Issuing
a new token invalidates the previous one.

**Account enumeration.**

- Register, resend-verification and forgot-password always return the same 202
  response.
- Registering with an existing address emails the owner instead of creating an
  account.
- Login hashes a dummy password for unknown emails and returns one generic error.
- Emails are sent after the response, via `BackgroundTasks`, so SMTP latency
  doesn't leak whether an account exists.

**Rate limiting.** Fixed-window counters in Redis, with IPs and emails hashed in
the keys:

| Endpoint | Limit |
|---|---|
| Login | per IP and per email |
| Other auth endpoints | per IP, plus per email where an email is supplied |
| Refresh | per IP |
| Password change | per user |

All limits are configurable. Exceeding one returns 429 with `Retry-After`. If
Redis is unavailable the limiter **fails open** and logs an error.

**Email delivery.** SMTP adapter. Mailpit catches all mail in development.
Production requires STARTTLS or SSL.

## Alternatives

- *Refresh token in localStorage / response body:* readable by any XSS payload.
- *Stateful server sessions only:* simpler revocation, but every API call needs
  a session lookup. We need a database lookup anyway (to load the user), and
  `auth_version` gives immediate revocation, so stateless JWTs cost nothing extra.
- *RS256/EdDSA:* useful once other services verify tokens. Not needed while one
  API is both issuer and verifier. Switching later is a configuration and key
  management change.
- *Account lockout after N failures:* lets attackers lock out real users.
  Per-email throttling slows guessing without that denial of service.

## Consequences and residual risks

- Logging out one device leaves that device's access token valid for up to
  15 minutes; the client discards it. "Sign out everywhere" is immediate.
- Rate limiting fails open during a Redis outage; Argon2 cost still bounds
  guessing speed.
- `forgot-password` does slightly more database work for existing accounts.
  The timing difference is small and email latency is excluded, but it isn't zero.
- Non-browser HTTP clients must handle `Secure` cookies themselves. Browsers
  accept them on `http://localhost` in development.
- The SPA must make concurrent refresh calls single-flight (Phase 7).
