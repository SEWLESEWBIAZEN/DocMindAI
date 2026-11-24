/**
 * In-memory access token with silent refresh.
 *
 * The access token never touches storage (no XSS-readable persistence). The
 * refresh token is an HttpOnly, SameSite=Strict cookie scoped to /api/v1/auth;
 * refreshing rotates it. Concurrent callers share one refresh request.
 */
import axios from 'axios'

import type { Schemas } from './types'

export const CSRF_HEADER = 'X-CSRF-Protection'
const REFRESH_MARGIN_MS = 30_000

type TokenResponse = Schemas['TokenResponse']
type Listener = (event: SessionEvent) => void
export type SessionEvent = { type: 'signed-in'; user: Schemas['UserResponse'] } | { type: 'expired' }

let accessToken: string | null = null
let expiresAt = 0
let inflight: Promise<TokenResponse> | null = null
const listeners = new Set<Listener>()

/** Bare client: never intercepted, so refreshing can't recurse. */
const authClient = axios.create({
  baseURL: '/api/v1/auth',
  timeout: 15_000,
  headers: { Accept: 'application/json', [CSRF_HEADER]: '1' },
  withCredentials: true,
})

export function setSession(token: TokenResponse): void {
  accessToken = token.access_token
  expiresAt = Date.now() + token.expires_in * 1000
  emit({ type: 'signed-in', user: token.user })
}

export function clearSession(): void {
  accessToken = null
  expiresAt = 0
}

export function currentToken(): string | null {
  return accessToken
}

export function onSessionEvent(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function emit(event: SessionEvent): void {
  for (const listener of listeners) listener(event)
}

/** Rotate the refresh cookie for a new access token. Shared by concurrent callers. */
export function refreshSession(): Promise<TokenResponse> {
  inflight ??= authClient
    .post<TokenResponse>('/refresh')
    .then((response) => {
      setSession(response.data)
      return response.data
    })
    .finally(() => {
      inflight = null
    })
  return inflight
}

/** Signal that the session can't be recovered (refresh failed). */
export function expireSession(): void {
  const hadSession = accessToken !== null
  clearSession()
  if (hadSession) emit({ type: 'expired' })
}

/** A token valid for at least the next 30 s, refreshing if needed; null if signed out. */
export async function validAccessToken(): Promise<string | null> {
  if (accessToken && Date.now() < expiresAt - REFRESH_MARGIN_MS) return accessToken
  try {
    return (await refreshSession()).access_token
  } catch {
    expireSession()
    return null
  }
}

export { authClient }
