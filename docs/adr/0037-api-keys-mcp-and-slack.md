# ADR 0037: Workspace API keys, an MCP server and a Slack app

- Status: Accepted
- Date: 2026-11-26
- Builds on: ADR 0022 (credential vault), ADR 0026 (2FA), ADR 0032 (plans), ADR 0036 (restricted collections)

## Context

Teams want their documents where they already work: in scripts and automations, in AI
assistants (Claude, ChatGPT, Cursor and others speak the Model Context Protocol), and in
Slack. Each of these is a new way in, so each must keep the guarantees the app makes:
- tenant isolation;
- roles;
- restricted collections;
- quotas;
- audit.

## Decision

### API keys

- **Plans:** Pro and above, including the free Pro trial (`INTEGRATION_PLANS`). A workspace
  that drops to Free stops accepting its keys (`403 integrations_plan_required`).
- **Creating:** admins and owners create keys, with a recent 2FA check. A key has a name,
  scopes and an expiry (30, 90 or 365 days, or never).
  - The token (`dmk_` plus 256 random bits) is shown once and stored as a SHA-256 hash.
  - At most 25 active keys per workspace.
  - Creating and revoking are audited.
- **Scopes:**
  - `documents:read`: list, read and search documents and collections.
  - `documents:write`: also upload and organize.
  - `ask`: cited answers and conversations.
- **What a key can do:** it acts as its creator, in its own workspace only. Its permissions
  are the intersection of its scopes and the creator's current role. Workspace admin
  permissions are never granted to a key, so:
  - an admin's key can't manage members, billing, settings, keys, SSO or infrastructure;
  - it sees a restricted collection only if its creator is a member of that collection.
- **Where keys work:** only on workspace document, collection, search, ask, conversation,
  extraction, deadline and MCP routes (`route_allowed`). Everything else, including
  `/me`, returns `403 api_key_not_allowed`. Another workspace returns `404`.
- **When a key stops:** if the creator leaves the workspace or is deactivated, the key
  stops. Changing their password does not stop it: integrations shouldn't break when
  someone rotates a password. Revoke the key instead.
- **Limits:** each key has its own rate limit (`RATE_LIMIT_API_KEY_PER_MINUTE`). AI requests
  also count against the creator's AI budget and the workspace's answer quota.

### MCP server

- **Endpoint:** `POST /api/v1/workspaces/{id}/mcp`, using the Streamable HTTP transport.
  - It is stateless and answers with JSON, never SSE, because it has nothing to push.
  - `GET` returns 405. Notifications return 202.
  - Batches are accepted for protocol 2025-03-26 clients.
  - Supported versions are 2025-11-25, 2025-06-18 and 2025-03-26. An unsupported
    `MCP-Protocol-Version` header returns 400.
  - A browser `Origin` other than the app's own is refused, to block DNS rebinding.
- **Tools:** `search`, `ask`, `list_documents`, `read_document` and `list_collections`.
  - All are read-only (annotated `readOnlyHint`).
  - Each returns readable text and `structuredContent`, with links back into the app.
  - `tools/list` shows only the tools the key's scopes allow.
  - They call the same services as the REST API, so permissions, restricted collections and
    quotas apply unchanged. A hidden document is reported as not found.
- **Authentication:** a bearer API key, or a signed-in user's access token. OAuth
  authorization for MCP clients isn't offered yet.
- **Compatibility:** checked with the official MCP Python SDK (2.3.0), which negotiated
  2025-11-25 and called the tools.

### Slack app

- **Setup and install:**
  - The server operator registers one Slack app (`SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`,
    `SLACK_SIGNING_SECRET`). When they are unset, Slack isn't offered.
  - Admins install the app with a recent 2FA check, using OAuth v2 with bot scopes
    `commands`, `app_mentions:read` and `chat:write`.
  - The callback uses a single-use state in Redis and is bound to the browser that started
    it with an HttpOnly cookie, the same as the Drive connector and SSO.
  - The bot token is kept in the credential vault (kind `slack_bot_token`).
  - One Slack team maps to one DocuMind workspace.
- **Every request from Slack** is verified with the signing secret, using HMAC-SHA256 over
  `v0:{timestamp}:{body}` within a five-minute window, before anything is read. Events are
  de-duplicated by `event_id`, because Slack retries.
- **Linking accounts:**
  - A Slack user is linked to a DocuMind account with a code. The person makes the code in
    DocuMind while signed in, then types `/documind link CODE` in Slack.
  - Codes are 8 characters, single-use, last 10 minutes, and attempts are rate-limited.
  - The code proves the DocuMind account. The reverse design (a link from Slack opened in
    DocuMind) would let someone send their own link to a colleague and borrow that
    colleague's access.
  - Email matching isn't used, because a Slack email doesn't prove DocuMind membership.
- **Answers:**
  - `/documind question` replies privately (`ephemeral`), using everything the asker may see.
  - `@DocuMind question` replies in the channel's thread, using only documents everyone in
    the workspace may see. The context is marked `public_only`, so `viewer_of` belongs to no
    restricted collection. A channel can include people who shouldn't see HR or legal
    files.
  - Slack expects a reply within three seconds. The endpoint acknowledges at once, and the
    worker (`documind.slack_answer`) answers through the same retrieval and answer
    pipeline, with the asker's membership, 2FA policy and the answer quota checked.
  - Replies cite their sources with links into DocuMind. Slack markup in answers and
    titles is escaped, so a document can't inject mentions or links.
  - Replies to slash commands go only to `https://hooks.slack.com`.
- **Removing the app:** removing it (2FA) or uninstalling it from Slack revokes the token,
  destroys the vault entry and forgets the links.

### Storage

`api_keys`, `slack_installations` and `slack_user_links` are control-plane tables. They hold
no document content.

## Consequences

- **Tests:** Slack was tested against a fake Slack Web API with real request signing. It
  hasn't been run against a live Slack workspace from this sandbox.
- **Not yet built:**
  - OAuth for MCP clients;
  - per-key audit of individual reads;
  - Microsoft Teams.
