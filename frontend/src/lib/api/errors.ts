import { isAxiosError } from 'axios'

/** Normalised error shape for every failed API call. */
export class ApiError extends Error {
  readonly status: number | null
  readonly requestId: string | null

  constructor(message: string, status: number | null, requestId: string | null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.requestId = requestId
  }
}

function detailFrom(data: unknown): string | null {
  if (data && typeof data === 'object' && 'detail' in data) {
    const { detail } = data as { detail: unknown }
    if (typeof detail === 'string') return detail
  }
  return null
}

export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error
  if (isAxiosError(error)) {
    const status = error.response?.status ?? null
    const header = error.response?.headers?.['x-request-id']
    const requestId = typeof header === 'string' ? header : null
    const message =
      detailFrom(error.response?.data) ??
      (status === null ? 'Unable to reach the server. Check your connection.' : `Request failed (${status})`)
    return new ApiError(message, status, requestId)
  }
  return new ApiError(error instanceof Error ? error.message : 'Unexpected error', null, null)
}
