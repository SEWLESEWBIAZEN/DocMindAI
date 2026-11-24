import type { AxiosAdapter, InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { apiClient } from './client'
import { ApiError } from './errors'
import { authClient, clearSession, currentToken, onSessionEvent, setSession } from './session'

const user = {
  id: 'u1',
  email: 'a@example.com',
  display_name: 'A',
  email_verified: true,
  mfa_enabled: false,
  default_workspace_id: null,
  created_at: '2026-01-01T00:00:00Z',
}

function respond(config: InternalAxiosRequestConfig, status: number, data: unknown) {
  const response = { data, status, statusText: '', headers: {}, config }
  if (status >= 400) {
    return Promise.reject(Object.assign(new Error('failed'), { isAxiosError: true, config, response }))
  }
  return Promise.resolve(response)
}

afterEach(() => {
  clearSession()
  apiClient.defaults.adapter = undefined
  authClient.defaults.adapter = undefined
})

describe('apiClient', () => {
  it('attaches the session token and the CSRF header', async () => {
    setSession({ access_token: 'tok-1', token_type: 'bearer', expires_in: 900, user })
    const seen: InternalAxiosRequestConfig[] = []
    apiClient.defaults.adapter = ((config) => {
      seen.push(config)
      return respond(config, 200, {})
    }) as AxiosAdapter
    await apiClient.get('/me')
    expect(seen[0]?.headers.get('Authorization')).toBe('Bearer tok-1')
    expect(seen[0]?.headers.get('X-CSRF-Protection')).toBe('1')
  })

  it('refreshes once for concurrent 401s and retries with the new token', async () => {
    setSession({ access_token: 'old', token_type: 'bearer', expires_in: 900, user })
    const refresh = vi.fn((config: InternalAxiosRequestConfig) =>
      respond(config, 200, { access_token: 'new', token_type: 'bearer', expires_in: 900, user }),
    )
    authClient.defaults.adapter = refresh as unknown as AxiosAdapter
    apiClient.defaults.adapter = ((config) =>
      config.headers.get('Authorization') === 'Bearer new'
        ? respond(config, 200, { ok: true })
        : respond(config, 401, { detail: 'expired' })) as AxiosAdapter

    const [a, b] = await Promise.all([apiClient.get('/a'), apiClient.get('/b')])
    expect(a.data).toEqual({ ok: true })
    expect(b.data).toEqual({ ok: true })
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(currentToken()).toBe('new')
  })

  it('expires the session when refreshing fails', async () => {
    setSession({ access_token: 'old', token_type: 'bearer', expires_in: 900, user })
    authClient.defaults.adapter = ((config) => respond(config, 401, { detail: 'no' })) as AxiosAdapter
    apiClient.defaults.adapter = ((config) => respond(config, 401, { detail: 'expired' })) as AxiosAdapter
    const events: string[] = []
    const off = onSessionEvent((event) => events.push(event.type))
    await expect(apiClient.get('/me')).rejects.toBeInstanceOf(ApiError)
    off()
    expect(events).toContain('expired')
    expect(currentToken()).toBeNull()
  })

  it('never refreshes for requests carrying their own token (2FA step-up)', async () => {
    setSession({ access_token: 'session', token_type: 'bearer', expires_in: 900, user })
    const refresh = vi.fn()
    authClient.defaults.adapter = refresh as unknown as AxiosAdapter
    const seen: string[] = []
    apiClient.defaults.adapter = ((config) => {
      seen.push(String(config.headers.get('Authorization')))
      return respond(config, 401, { detail: 'step-up expired' })
    }) as AxiosAdapter
    await expect(
      apiClient.delete('/x', { headers: { Authorization: 'Bearer elevated' } }),
    ).rejects.toMatchObject({ status: 401 })
    expect(seen).toEqual(['Bearer elevated'])
    expect(refresh).not.toHaveBeenCalled()
  })

  it('does not attach tokens to public endpoints', async () => {
    setSession({ access_token: 'tok', token_type: 'bearer', expires_in: 900, user })
    const seen: InternalAxiosRequestConfig[] = []
    apiClient.defaults.adapter = ((config) => {
      seen.push(config)
      return respond(config, 200, {})
    }) as AxiosAdapter
    await apiClient.post('/auth/register', {}, { skipAuth: true })
    expect(seen[0]?.headers.get('Authorization')).toBeFalsy()
  })
})
