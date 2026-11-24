# ADR 0022: Credential vault (envelope encryption and rotation)

- Status: Accepted
- Date: 2026-11-24
- Phase: 8
- Amends: ADR 0019 (BYOK key storage)

## Context

Phase 8 introduces several kinds of customer secrets:

- AI provider keys (BYOK);
- database passwords (dedicated and external data planes);
- object-storage credentials;
- later, customer worker tokens.

ADR 0019 proposed `SecretBox` with a separate key. Several kinds of secret, and the need to rotate the
encryption key without downtime, call for one vault with envelope encryption.

## Decision

- **Table:** a control-plane `credentials` table. Each row holds:
  - `id`, `workspace_id`, `kind` (`ai_provider_key`, `database_password`, `storage_secret`);
  - `ciphertext`, `wrapped_dek`, `kek_id`;
  - a non-secret `hint` (last 4 characters);
  - `status` (`active`, `revoked`);
  - `created_by`, `created_at`, `rotated_at`, `last_used_at`.
- **Envelope encryption:**
  - Each credential has its own random 256-bit data key (DEK), used with AES-256-GCM.
  - The DEK is wrapped with a key-encryption key (KEK) from a key ring.
  - The associated data binds `credential id`, `workspace id` and `kind`, so a ciphertext copied to
    another row or workspace fails to decrypt.
- **Key ring:** `CREDENTIAL_ENCRYPTION_KEYS="k2:<base64>,k1:<base64>"`. The first key wraps new DEKs, and
  all keys can unwrap.
  - Rotation: add a new key first, run `documind-manage credentials rewrap` (it rewraps DEKs only; the
    secrets themselves aren't re-encrypted), then drop the old key.
  - Production refuses to start with a development key or a ring shorter than 32 bytes per key.
- **KMS port:** `KeyEncryptionProvider` (`wrap`, `unwrap`) has a local key-ring implementation. AWS KMS,
  GCP KMS or HashiCorp Vault adapters can be added without changing callers (Phase 10 decision).
- **Write-only:**
  - The API accepts secrets and returns only the hint and metadata.
  - Secrets are decrypted in memory just before use, and never logged or put in exceptions. Adapters
    raise sanitized errors.
  - The worker decrypts with the same key ring.
- **Lifecycle:**
  - Replacing a secret creates a new credential and revokes the old one, so a pinned profile version
    keeps a working reference until the new version is activated.
  - Deleting a workspace deletes its credentials.
  - Backups contain only ciphertext. Keys are backed up separately (Phase 10 runbook).
- **Use tracking:** `last_used_at` is updated at most once an hour per credential.

## Consequences

- One code path to review for every customer secret (Phase 9 BYOK and infrastructure security review).
- Losing every KEK makes customer credentials unrecoverable. Customers then re-enter them; their data is
  unaffected.
- BYOK keeps ADR 0019's properties: AES-GCM, a separate key from 2FA secrets, binding to the
  workspace, and write-only handling.
