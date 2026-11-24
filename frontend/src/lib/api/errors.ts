import { isAxiosError } from 'axios'

/** Normalised error shape for every failed API call. */
export class ApiError extends Error {
  readonly status: number | null
  readonly requestId: string | null
  /** Stable machine-readable code from the backend (e.g. `reply_in_progress`). */
  readonly code: string | null
  /** Field errors from 422 validation responses, keyed by field name. */
  readonly fields: Record<string, string>

  constructor(
    message: string,
    status: number | null,
    requestId: string | null,
    code: string | null = null,
    fields: Record<string, string> = {},
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.requestId = requestId
    this.code = code
    this.fields = fields
  }
}

interface ValidationItem {
  loc?: unknown[]
  msg?: unknown
}

function parseBody(data: unknown): { message: string | null; code: string | null; fields: Record<string, string> } {
  const fields: Record<string, string> = {}
  if (!data || typeof data !== 'object') return { message: null, code: null, fields }
  const body = data as { detail?: unknown; code?: unknown }
  const code = typeof body.code === 'string' ? body.code : null
  if (typeof body.detail === 'string') return { message: body.detail, code, fields }
  if (Array.isArray(body.detail)) {
    for (const item of body.detail as ValidationItem[]) {
      const loc = Array.isArray(item.loc) ? item.loc : []
      const field = String(loc[loc.length - 1] ?? '')
      if (field && typeof item.msg === 'string' && !(field in fields)) {
        fields[field] = item.msg.replace(/^Value error, /, '')
      }
    }
    const first = Object.values(fields)[0]
    return { message: first ? `Please check the form: ${first}` : 'Please check the form.', code: code ?? 'validation_failed', fields }
  }
  return { message: null, code, fields }
}

export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error
  if (isAxiosError(error)) {
    const status = error.response?.status ?? null
    const header = error.response?.headers?.['x-request-id']
    const requestId = typeof header === 'string' ? header : null
    const { message, code, fields } = parseBody(error.response?.data)
    const fallback =
      status === null
        ? 'Unable to reach the server. Check your connection.'
        : status === 429
          ? 'Too many requests. Please wait a moment and try again.'
          : status >= 500
            ? 'Something went wrong on our side. Please try again.'
            : `Request failed (${status})`
    return new ApiError(message ?? fallback, status, requestId, code, fields)
  }
  return new ApiError(error instanceof Error ? error.message : 'Unexpected error', null, null)
}

/** Human-readable message for any thrown value. */
export function errorMessage(error: unknown): string {
  return toApiError(error).message
}

export function isStatus(error: unknown, status: number): boolean {
  return error instanceof ApiError && error.status === status
}
