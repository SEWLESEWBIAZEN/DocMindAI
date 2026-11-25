/**
 * Passkeys (WebAuthn, ADR 0029): turn the server's JSON options into browser
 * calls and the browser's credentials back into JSON. No library needed.
 */

export type Json = Record<string, unknown>

interface Descriptor {
  id: string
  type: 'public-key'
  transports?: AuthenticatorTransport[]
}

function fromB64url(value: string): ArrayBuffer {
  const base64 = value.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(value.length / 4) * 4, '=')
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return bytes.buffer
}

function toB64url(buffer: ArrayBuffer | null): string | null {
  if (!buffer) return null
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

const descriptors = (list: unknown): PublicKeyCredentialDescriptor[] =>
  ((list as Descriptor[] | undefined) ?? []).map((d) => ({ ...d, id: fromB64url(d.id) }))

export function passkeysSupported(): boolean {
  return typeof window !== 'undefined' && 'PublicKeyCredential' in window && !!navigator.credentials
}

/** Register a passkey: navigator.credentials.create(). */
export async function createPasskey(options: Json): Promise<Json> {
  const user = options.user as { id: string; name: string; displayName: string }
  const publicKey = {
    ...options,
    challenge: fromB64url(options.challenge as string),
    user: { ...user, id: fromB64url(user.id) },
    excludeCredentials: descriptors(options.excludeCredentials),
  } as unknown as PublicKeyCredentialCreationOptions
  const credential = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential | null
  if (!credential) throw new Error('No passkey was created.')
  const response = credential.response as AuthenticatorAttestationResponse
  return {
    id: credential.id,
    rawId: toB64url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {
      clientDataJSON: toB64url(response.clientDataJSON),
      attestationObject: toB64url(response.attestationObject),
      transports: typeof response.getTransports === 'function' ? response.getTransports() : [],
    },
  }
}

/** Confirm with a passkey: navigator.credentials.get(). */
export async function getPasskey(options: Json): Promise<Json> {
  const publicKey = {
    ...options,
    challenge: fromB64url(options.challenge as string),
    allowCredentials: descriptors(options.allowCredentials),
  } as unknown as PublicKeyCredentialRequestOptions
  const credential = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential | null
  if (!credential) throw new Error('No passkey was used.')
  const response = credential.response as AuthenticatorAssertionResponse
  return {
    id: credential.id,
    rawId: toB64url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {
      clientDataJSON: toB64url(response.clientDataJSON),
      authenticatorData: toB64url(response.authenticatorData),
      signature: toB64url(response.signature),
      userHandle: toB64url(response.userHandle),
    },
  }
}

/** A friendly message for a cancelled or failed browser prompt. */
export function passkeyErrorMessage(err: unknown): string | null {
  if (err instanceof DOMException) {
    if (err.name === 'NotAllowedError') return 'The passkey prompt was closed or timed out. Try again.'
    if (err.name === 'InvalidStateError') return 'This device already has a passkey for your account.'
    return 'Your browser could not use a passkey here.'
  }
  return null
}
