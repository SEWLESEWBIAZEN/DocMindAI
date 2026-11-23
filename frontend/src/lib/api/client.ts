import axios from 'axios'

import { toApiError } from './errors'

/**
 * Shared HTTP client. All requests go to the same origin under /api/v1; in
 * development Vite proxies them to the backend, in production Nginx does.
 * Authentication interceptors are added in Phase 2.
 */
export const apiClient = axios.create({
  baseURL: '/api/v1',
  timeout: 15_000,
  headers: { Accept: 'application/json' },
})

apiClient.interceptors.response.use(
  (response) => response,
  (error: unknown) => Promise.reject(toApiError(error)),
)
