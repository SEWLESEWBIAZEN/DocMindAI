/** Mirrors `HealthResponse` in backend/src/documind/api/routes/health.py. */
export type ComponentStatus = 'ok' | 'error'
export type OverallStatus = 'ok' | 'degraded'

export interface ComponentHealth {
  status: ComponentStatus
  latency_ms: number
  details: Record<string, string | number | boolean | null> | null
  error: string | null
}

export interface HealthResponse {
  status: OverallStatus
  timestamp: string
  app: { name: string; version: string; environment: string }
  components: Record<string, ComponentHealth>
}
