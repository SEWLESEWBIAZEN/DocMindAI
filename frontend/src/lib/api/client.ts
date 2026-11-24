import axios, { type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios'

import { toApiError } from './errors'
import { CSRF_HEADER, currentToken, expireSession, refreshSession } from './session'

declare module 'axios' {
  interface AxiosRequestConfig {
    /** Don't attach the session token (public endpoints). */
    skipAuth?: boolean
  }
}

/** `_session`: we attached the session token (so a 401 may be fixed by refreshing). */
type RetriableConfig = InternalAxiosRequestConfig & { _retried?: boolean; _session?: boolean }

/**
 * Shared HTTP client. All requests go to the same origin under /api/v1; in
 * development Vite proxies them to the backend, in production Nginx does.
 *
 * - attaches the in-memory access token (unless the request carries its own,
 *   e.g. a short-lived 2FA step-up token);
 * - on 401, refreshes once (shared across concurrent requests) and retries;
 * - normalises every failure into an `ApiError`.
 */
export const apiClient = axios.create({
  baseURL: '/api/v1',
  timeout: 30_000,
  headers: { Accept: 'application/json', [CSRF_HEADER]: '1' },
})

apiClient.interceptors.request.use((config: RetriableConfig) => {
  const token = currentToken()
  if (!config.skipAuth && (config._session || !config.headers.has('Authorization'))) {
    if (token) config.headers.set('Authorization', `Bearer ${token}`)
    config._session = true
  }
  return config
})

apiClient.interceptors.response.use(
  (response) => response,
  async (error: unknown) => {
    const config = (axios.isAxiosError(error) ? error.config : undefined) as RetriableConfig | undefined
    const status = axios.isAxiosError(error) ? error.response?.status : undefined
    if (status === 401 && config?._session && !config._retried) {
      config._retried = true
      try {
        await refreshSession()
        return apiClient.request(config as AxiosRequestConfig)
      } catch {
        expireSession()
      }
    }
    return Promise.reject(toApiError(error))
  },
)
