# ADR 0019: Bring your own API key (BYOK)

- Status: Proposed (roadmap). Scheduled for Phase 8; security review in Phase 9.
- Date: 2026-11-24

## Context

The product owner wants users to be able to use their own AI provider API key.
Reasons include:

- cost control: usage is billed to their own provider account;
- data agreements: a zero-retention or enterprise contract with the provider;
- higher rate limits;
- using DocuMind before a paid plan exists.

Today one platform key (`OPENAI_API_KEY`) serves everything: embeddings,
answers, follow-up condensation, figure descriptions, speech-to-text and
text-to-speech.

## Decision (proposed)

### Who owns a key

- **Personal workspace:** the owner can add a key, used for everything in that
  workspace.
- **Organization:** owners and admins add a **workspace key**. It is used for
  all members' activity, including background processing, which has no user
  attached.
- Per-member keys inside an organization are **not** supported. Documents are
  indexed once, so embeddings must come from one consistent source per
  workspace, and mixing keys would make cost and data-processing terms
  unclear.

### Storage and handling

- Keys are encrypted at rest with AES-GCM, using the existing `SecretBox`
  pattern. The encryption key is separate (`PROVIDER_KEY_ENCRYPTION_KEY`), and
  the ciphertext is bound to the workspace id, so a copied row won't decrypt.
- Keys are **write-only**: after saving, the API and UI show only the provider,
  the last 4 characters, who added it and when, and when it was last used.
- Keys are never logged and never sent to the browser. Error messages from the
  provider are sanitised so a key can't leak through them.
- A key is decrypted only in memory for the duration of a provider call (in
  the API and in the worker).
- Saving a key requires a fresh 2FA step-up (as for permanent deletion) and is
  recorded in the audit log (added, replaced, removed, failed validation).
- On save, a cheap validation call checks that the key works and has access to
  the configured models. Invalid keys are rejected with the provider's
  (sanitised) reason.

### Providers

- Start with **OpenAI**, the same adapters as today with the key injected per
  call.
- Later: Azure OpenAI and OpenAI-compatible endpoints. A custom base URL is a
  server-side request forgery (SSRF) risk, so it will be limited to an
  admin-managed allowlist of hostnames, HTTPS only, with no private or
  link-local addresses.

### Resolution and fallback

- Provider calls resolve the key per workspace:
  1. workspace key (if set and active);
  2. platform key (if the workspace's plan allows platform usage);
  3. otherwise a clear "No AI key configured" error.
- A workspace can choose **"Use my key only"**, with no silent fallback to the
  platform key. Organizations with data agreements need this guarantee.
- If the provider rejects the workspace key (revoked, quota exceeded):
  - processing jobs fail with `provider_key_invalid` (not retried
    indefinitely);
  - questions return a clear error;
  - admins see a banner and receive an email.
- Changing the key **within the same embedding model** needs nothing else.
  Changing provider or embedding model triggers re-indexing
  (`documind-manage reindex`, surfaced in the UI with a cost estimate).

### Usage and plans (Phase 8)

- Usage records gain a `billed_to` field: `platform` or `workspace_key`.
  Calls made with the workspace's own key don't count toward platform quotas,
  but are still recorded for visibility (tokens, audio minutes).
- Plans decide whether BYOK is offered and whether platform fallback is
  allowed.

### Interaction with voice (ADR 0011 / 0018)

- Server speech uses the workspace key when one is set.
- The workspace voice policy still applies: a key doesn't override "Browser
  voice only" or "Off".

## Alternatives considered

- **Keys stored only in the browser, sent with each request.** Rejected:
  background processing (embeddings, figure descriptions) needs the key with
  no browser present, and the key would sit in browser storage exposed to any
  XSS.
- **Per-member keys in organizations.** Rejected for now (see above).
- **A secrets manager (Vault, cloud KMS).** A better fit for production at
  scale; the storage interface is designed so it can be swapped in later
  (Phase 10).

## Consequences

- A new class of secret to protect: encryption key rotation, deletion when a
  workspace or account is deleted, and backups that contain only ciphertext.
- Support load: "my key stopped working" becomes a user-facing error that
  must be clear and actionable.
- Answer quality and cost now depend partly on the customer's provider
  account (rate limits, model access).

## Open questions for the product owner

1. Should **organizations** be able to forbid the platform key entirely
   (BYOK-only), or is the "Use my key only" setting enough?
2. Which providers beyond OpenAI matter first (Azure OpenAI, Anthropic, local
   models)?
3. Should BYOK be available on the Free plan?
