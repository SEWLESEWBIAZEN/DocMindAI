import { apiClient } from '../../lib/api/client'
import type { HealthResponse } from './types'

/** A degraded system answers 503 with a full report, which is still a valid result. */
export async function fetchHealth(): Promise<HealthResponse> {
  const response = await apiClient.get<HealthResponse>('/health', {
    validateStatus: (status) => status === 200 || status === 503,
  })
  return response.data
}
