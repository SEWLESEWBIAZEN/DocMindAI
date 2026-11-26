/** API keys, the MCP server and the Slack app (ADR 0037). */
import { apiClient } from '../../lib/api/client'
import type { Schemas } from '../../lib/api/types'

export type Integrations = Schemas['IntegrationsResponse']
export type ApiKey = Schemas['ApiKeyResponse']
export type NewApiKey = Schemas['NewApiKeyResponse']
export type ApiKeyRequest = Schemas['ApiKeyRequest']
export type LinkCode = Schemas['LinkCodeResponse']

const ws = (id: string) => `/workspaces/${id}`
const elevated = (token: string) => ({ headers: { Authorization: `Bearer ${token}` } })

export const integrationsKey = (id: string) => ['ws', id, 'integrations'] as const
export const keysKey = (id: string) => ['ws', id, 'api-keys'] as const

export async function getIntegrations(id: string): Promise<Integrations> {
  return (await apiClient.get<Integrations>(`${ws(id)}/integrations`)).data
}

export async function listKeys(id: string): Promise<ApiKey[]> {
  return (await apiClient.get<ApiKey[]>(`${ws(id)}/api-keys`)).data
}

export async function createKey(id: string, body: ApiKeyRequest, token: string): Promise<NewApiKey> {
  return (await apiClient.post<NewApiKey>(`${ws(id)}/api-keys`, body, elevated(token))).data
}

export async function revokeKey(id: string, keyId: string): Promise<void> {
  await apiClient.delete(`${ws(id)}/api-keys/${keyId}`)
}

export async function authorizeSlack(id: string, token: string): Promise<string> {
  return (await apiClient.post<{ url: string }>(`${ws(id)}/integrations/slack/authorize`, null, elevated(token))).data.url
}

export async function removeSlack(id: string, token: string): Promise<void> {
  await apiClient.delete(`${ws(id)}/integrations/slack`, elevated(token))
}

export async function linkCode(id: string): Promise<LinkCode> {
  return (await apiClient.post<LinkCode>(`${ws(id)}/integrations/slack/link-code`)).data
}

export async function unlinkSlack(id: string): Promise<void> {
  await apiClient.delete(`${ws(id)}/integrations/slack/link`)
}

export const SLACK_ERRORS: Record<string, string> = {
  denied: 'Adding the Slack app was cancelled.',
  expired: 'The request expired. Try again.',
  browser: 'Finish adding the app in the same browser you started in.',
  failed: "Slack didn't accept the request. Try again.",
  team_taken: 'That Slack workspace is already connected to another DocuMind workspace.',
}

/** Configuration for MCP clients that take a JSON file (Claude Desktop, Cursor and others). */
export function mcpConfig(url: string): string {
  return JSON.stringify(
    { mcpServers: { documind: { type: 'http', url, headers: { Authorization: 'Bearer YOUR_API_KEY' } } } },
    null,
    2,
  )
}
