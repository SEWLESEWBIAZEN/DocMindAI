# ADR 0036: Single sign-on, SCIM provisioning and per-collection access

- Status: Accepted
- Date: 2026-11-26
- Builds on: ADR 0013/0026 (2FA step-up and enforcement), ADR 0022 (credential vault), ADR 0032 (plans)

## Context

Larger customers buy only if:
- their identity provider (Okta, Entra ID, Google Workspace, Keycloak and others) controls who
  signs in;
- leavers lose access automatically;
- sensitive material, such as HR or legal files, can be limited to some people inside the
  organization.

## Decision

### Single sign-on (OpenID Connect)

- **Plans:** Business and Enterprise organizations. Personal workspaces don't get SSO.
- **Setting it up:** admins and owners, with a recent 2FA check, enter:
  - the issuer URL;
  - the client ID and secret. The secret is stored in the credential vault (kind
    `sso_client_secret`) and never returned;
  - the email domains;
  - the role for new members (member or viewer);
  - whether SSO is **required**.

  The issuer is checked by fetching its discovery document. That request goes through the same
  SSRF guard as other customer URLs.
- **Domain verification:** a connection starts as `pending_verification`. An operator approves it
  after confirming the customer owns the domains, with `documind-manage sso approve <id>
  --operator <name> --note <evidence>`. Without this step, any organization could claim
  `@bigcorp.com` and take over sign-in for its people.
  - Changing the issuer, client or domains sends the connection back for verification.
  - Public email domains (gmail.com, outlook.com and others) are refused.
  - A domain can be active for only one organization.
- **Signing in:** "Sign in with single sign-on" asks for the email and redirects to the provider.
  - The flow is the authorization code flow with PKCE, a nonce, and a single-use `state` kept in
    Redis for ten minutes.
  - The callback is bound to the browser that started it by an HttpOnly, SameSite=Lax cookie,
    the same as the Drive connector.
  - The ID token is verified against the issuer's JWKS: signature, issuer, audience, expiry and
    nonce. The email must be `email_verified` and in one of the organization's domains.
- **New people** get an account (with a verified email and a personal workspace) and join the
  organization with the default role. That is just-in-time provisioning.
- **Required SSO:**
  - Password sign-in for people in the organization's domains is refused with `sso_required`.
  - A session started with SSO carries an `sso` claim naming the organization. That organization
    trusts its provider's MFA, so DocMind's own 2FA enforcement doesn't apply there. Other
    workspaces still apply their own 2FA rules.
  - Step-up checks for sensitive actions still apply.

### SCIM 2.0 provisioning

- **Endpoint:** `/api/v1/scim/v2/Users`, authenticated by a bearer token. The token is created
  by admins with 2FA, shown once, and stored as a hash. A new token replaces the old one.
- **Supported:** list (with `userName eq "…"`), get, create, replace, patch (`active`,
  `displayName`) and delete.
- **Leavers:** deactivating or deleting a user removes their membership in that organization
  only; their account and other workspaces are unaffected. The last owner can't be removed
  (`409`).
- **Audit:** every change is recorded.

### Per-collection access

- **Who:** admins and owners can mark a collection **restricted** and choose which members can
  see it.
- **The rule:** a document in a restricted collection is visible only to members of at least one
  of its restricted collections. Admins and owners see everything.
- **Where it applies:** the rule sits in the repositories (`viewer_of`, `document_visible`,
  `collection_visible`), so it covers:
  - document lists and detail pages, downloads, pages, chunks and versions;
  - collections;
  - search and answers, including retrieval for conversations;
  - summaries;
  - extraction runs and results, and deadlines;
  - duplicate-upload messages, which no longer reveal a hidden title.

  A hidden document returns `404`, as if it didn't exist.
- **Storage:** `collection_members` and `restricted` live in the data plane with the documents.

## Consequences

- **Removing a member** from a restricted collection takes effect on their next request; nothing
  is cached per user.
- **Tests:** SSO was tested with a fake OIDC provider that signs real RS256 tokens. It hasn't been
  run against a live provider from this sandbox.
- **SAML** isn't supported. Most providers offer OIDC; SAML can be added behind the same
  connection model.
- **SCIM groups** aren't mapped to collections yet. People are added to collections in the app.
