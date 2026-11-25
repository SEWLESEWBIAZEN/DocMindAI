import { afterEach, describe, expect, it, vi } from 'vitest'

import { createPasskey, getPasskey, passkeyErrorMessage } from './webauthn'

const bytes = (...values: number[]) => new Uint8Array(values).buffer

describe('webauthn JSON conversion', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('decodes base64url options and encodes the credential back', async () => {
    const create = vi.fn().mockResolvedValue({
      id: 'AQI',
      rawId: bytes(1, 2),
      type: 'public-key',
      authenticatorAttachment: 'platform',
      getClientExtensionResults: () => ({}),
      response: { clientDataJSON: bytes(251, 255), attestationObject: bytes(3), getTransports: () => ['internal'] },
    })
    vi.stubGlobal('navigator', { credentials: { create } })
    const result = await createPasskey({
      challenge: '-_8',
      user: { id: 'AQI', name: 'a@example.com', displayName: 'A' },
      excludeCredentials: [{ id: 'AQI', type: 'public-key' }],
    })
    const passed = create.mock.calls[0][0].publicKey
    expect(new Uint8Array(passed.challenge)).toEqual(new Uint8Array([251, 255]))
    expect(new Uint8Array(passed.excludeCredentials[0].id)).toEqual(new Uint8Array([1, 2]))
    expect(result.rawId).toBe('AQI')
    expect((result.response as Record<string, unknown>).clientDataJSON).toBe('-_8') // url-safe, unpadded
    expect((result.response as Record<string, unknown>).transports).toEqual(['internal'])
  })

  it('returns a null user handle when the authenticator gives none', async () => {
    vi.stubGlobal('navigator', {
      credentials: {
        get: vi.fn().mockResolvedValue({
          id: 'AQI',
          rawId: bytes(1, 2),
          type: 'public-key',
          authenticatorAttachment: null,
          getClientExtensionResults: () => ({}),
          response: { clientDataJSON: bytes(1), authenticatorData: bytes(2), signature: bytes(3), userHandle: null },
        }),
      },
    })
    const result = await getPasskey({ challenge: 'AQI', allowCredentials: [] })
    expect((result.response as Record<string, unknown>).userHandle).toBeNull()
  })

  it('explains a cancelled prompt', () => {
    expect(passkeyErrorMessage(new DOMException('x', 'NotAllowedError'))).toMatch(/closed or timed out/)
    expect(passkeyErrorMessage(new Error('x'))).toBeNull()
  })
})
