/** Plans and quotas (ADR 0020), AI provider keys (ADR 0019) and infrastructure (ADR 0021). */
import { apiClient } from '../../lib/api/client'
import type { Schemas } from '../../lib/api/types'

export type PlanSummary = Schemas['PlanSummaryResponse']
export type Quota = Schemas['QuotaResponse']
export type AIProviders = Schemas['AIProvidersResponse']
export type ProviderKey = Schemas['ProviderResponse']
export type Reindex = Schemas['ReindexResponse']
export type Infrastructure = Schemas['InfrastructureResponse']
export type InfrastructureVersion = Schemas['VersionResponse']
export type CreateVersion = Schemas['CreateVersionRequest']
export type WorkerToken = Schemas['WorkerTokenResponse']

const ws = (id: string) => `/workspaces/${id}`
const elevated = (token: string) => ({ headers: { Authorization: `Bearer ${token}` } })

export const planKey = (id: string) => ['ws', id, 'plan'] as const
export const providersKey = (id: string) => ['ws', id, 'ai-providers'] as const
export const infrastructureKey = (id: string) => ['ws', id, 'infrastructure'] as const

export async function getPlan(id: string): Promise<PlanSummary> {
  return (await apiClient.get<PlanSummary>(`${ws(id)}/plan`)).data
}

export async function getProviders(id: string): Promise<AIProviders> {
  return (await apiClient.get<AIProviders>(`${ws(id)}/ai-providers`)).data
}

export async function putProviderKey(
  id: string,
  provider: string,
  body: { api_key: string; settings?: Record<string, string>; reindex?: boolean },
  token: string,
): Promise<ProviderKey> {
  return (await apiClient.put<ProviderKey>(`${ws(id)}/ai-providers/${provider}`, body, elevated(token))).data
}

export async function removeProviderKey(id: string, provider: string, reindex: boolean, token: string): Promise<void> {
  await apiClient.delete(`${ws(id)}/ai-providers/${provider}`, { params: { reindex }, ...elevated(token) })
}

export async function revalidateProviderKey(id: string, provider: string): Promise<ProviderKey> {
  return (await apiClient.post<ProviderKey>(`${ws(id)}/ai-providers/${provider}/validate`)).data
}

export async function setKeyOnly(id: string, keyOnly: boolean, token?: string): Promise<boolean> {
  const { data } = await apiClient.put<{ key_only: boolean }>(
    `${ws(id)}/ai-settings`,
    { key_only: keyOnly },
    token ? elevated(token) : undefined,
  )
  return data.key_only
}

export async function getReindex(id: string): Promise<Reindex> {
  return (await apiClient.get<Reindex>(`${ws(id)}/reindex`)).data
}

export async function startReindex(id: string): Promise<Reindex> {
  return (await apiClient.post<Reindex>(`${ws(id)}/reindex`)).data
}

export async function getInfrastructure(id: string): Promise<Infrastructure> {
  return (await apiClient.get<Infrastructure>(`${ws(id)}/infrastructure`)).data
}

export async function createVersion(id: string, body: CreateVersion, token: string): Promise<InfrastructureVersion> {
  return (await apiClient.post<InfrastructureVersion>(`${ws(id)}/infrastructure/versions`, body, elevated(token))).data
}

export async function validateVersion(id: string, versionId: string): Promise<InfrastructureVersion> {
  return (await apiClient.post<InfrastructureVersion>(`${ws(id)}/infrastructure/versions/${versionId}/validate`)).data
}

export async function activateVersion(id: string, versionId: string, token: string): Promise<InfrastructureVersion> {
  return (
    await apiClient.post<InfrastructureVersion>(
      `${ws(id)}/infrastructure/versions/${versionId}/activate`,
      undefined,
      elevated(token),
    )
  ).data
}

export async function cancelMigration(id: string, migrationId: string): Promise<void> {
  await apiClient.post(`${ws(id)}/infrastructure/migrations/${migrationId}/cancel`)
}

export async function approveMigration(id: string, migrationId: string, token: string): Promise<{ queued: boolean }> {
  return (
    await apiClient.post<{ queued: boolean }>(
      `${ws(id)}/infrastructure/migrations/${migrationId}/approve`,
      undefined,
      elevated(token),
    )
  ).data
}

export async function cleanupMigration(id: string, migrationId: string, token: string): Promise<{ objects: number }> {
  return (
    await apiClient.post<{ objects: number }>(
      `${ws(id)}/infrastructure/migrations/${migrationId}/cleanup`,
      undefined,
      elevated(token),
    )
  ).data
}

export async function registerWorker(id: string, name: string, token: string): Promise<WorkerToken> {
  return (await apiClient.post<WorkerToken>(`${ws(id)}/infrastructure/workers`, { name }, elevated(token))).data
}

export async function revokeWorker(id: string, workerId: string): Promise<void> {
  await apiClient.delete(`${ws(id)}/infrastructure/workers/${workerId}`)
}

export const CAPABILITY_LABELS: Record<string, string> = {
  embedding: 'Search indexing',
  chat: 'Answers',
  vision: 'Figure descriptions',
  speech_to_text: 'Speech to text',
  text_to_speech: 'Text to speech',
}

export const ROUTE_LABELS: Record<string, string> = {
  workspace_key: "Your key",
  platform: "DocuMind's key",
  provider_key_required: 'Off: needs your key',
  provider_key_invalid: 'Paused: key rejected',
  capability_unavailable: 'Not available',
}

/** Human value for a quota metric. */
export function formatQuota(metric: string, value: number): string {
  if (metric === 'storage_bytes') {
    const units = ['B', 'KB', 'MB', 'GB', 'TB']
    let v = value
    let i = 0
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024
      i += 1
    }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`
  }
  if (metric === 'audio_seconds') {
    const minutes = value / 60
    return `${minutes >= 10 ? Math.round(minutes) : minutes.toFixed(1)} min`
  }
  return new Intl.NumberFormat().format(Math.round(value))
}

export function quotaPercent(q: Pick<Quota, 'limit' | 'used'>): number | null {
  if (q.limit === null || q.limit === undefined) return null
  if (q.limit === 0) return 100
  return Math.min(100, Math.round((q.used / q.limit) * 100))
}

// --- billing (ADR 0032) ------------------------------------------------------

export type BillingOverview = Schemas['BillingOverviewResponse']
export type PaidPlan = 'pro' | 'business'
export const billingKey = (id: string) => ['ws', id, 'billing'] as const

export async function getBilling(id: string): Promise<BillingOverview> {
  return (await apiClient.get<BillingOverview>(`${ws(id)}/billing`)).data
}

export async function startCheckout(id: string, plan: PaidPlan, interval: 'month' | 'year'): Promise<string> {
  return (await apiClient.post<{ url: string }>(`${ws(id)}/billing/checkout`, { plan, interval })).data.url
}

export async function changePlan(id: string, plan: PaidPlan, interval: 'month' | 'year'): Promise<BillingOverview> {
  return (await apiClient.post<BillingOverview>(`${ws(id)}/billing/plan`, { plan, interval })).data
}

/** Ask the server to read the subscription from Stripe now (after Checkout, or if a webhook is late). */
export async function syncBilling(id: string): Promise<BillingOverview> {
  return (await apiClient.post<BillingOverview>(`${ws(id)}/billing/sync`)).data
}

export async function openPortal(id: string): Promise<string> {
  return (await apiClient.post<{ url: string }>(`${ws(id)}/billing/portal`)).data.url
}
