import { AxiosError, AxiosHeaders } from 'axios'
import { describe, expect, it } from 'vitest'

import { ApiError, toApiError } from './errors'

function axiosError(status: number, data: unknown, requestId?: string): AxiosError {
  const headers = new AxiosHeaders(requestId ? { 'x-request-id': requestId } : {})
  return new AxiosError('failed', 'ERR_BAD_RESPONSE', undefined, undefined, {
    status,
    statusText: '',
    data,
    headers,
    config: { headers: new AxiosHeaders() },
  })
}

describe('toApiError', () => {
  it('uses the server detail and request id', () => {
    const error = toApiError(axiosError(500, { detail: 'Internal server error' }, 'abc123'))
    expect(error).toBeInstanceOf(ApiError)
    expect(error.message).toBe('Internal server error')
    expect(error.status).toBe(500)
    expect(error.requestId).toBe('abc123')
  })

  it('falls back to a generic message for non-string details', () => {
    expect(toApiError(axiosError(422, { detail: [{ msg: 'x' }] })).message).toBe('Request failed (422)')
  })

  it('reports network failures without a status', () => {
    const error = toApiError(new AxiosError('Network Error', 'ERR_NETWORK'))
    expect(error.status).toBeNull()
    expect(error.message).toMatch(/Unable to reach the server/)
  })

  it('passes ApiError through and wraps unknown errors', () => {
    const original = new ApiError('x', 400, null)
    expect(toApiError(original)).toBe(original)
    expect(toApiError(new Error('boom')).message).toBe('boom')
    expect(toApiError('weird').message).toBe('Unexpected error')
  })
})
