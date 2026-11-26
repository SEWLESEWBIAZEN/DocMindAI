/** Google Drive connector (ADR 0035). */
import { apiClient } from '../../lib/api/client'
import type { Schemas } from '../../lib/api/types'

export type Connectors = Schemas['ConnectorsResponse']
export type Connection = Schemas['ConnectionResponse']
export type Folder = Schemas['FolderResponse']

const base = (ws: string) => `/workspaces/${ws}/connectors`
const elevated = (token: string) => ({ headers: { Authorization: `Bearer ${token}` } })

export const connectorsKey = (ws: string) => ['ws', ws, 'connectors'] as const

export async function getConnectors(ws: string): Promise<Connectors> {
  return (await apiClient.get<Connectors>(base(ws))).data
}

export async function authorizeDrive(ws: string, token: string): Promise<string> {
  return (await apiClient.post<{ url: string }>(`${base(ws)}/google-drive/authorize`, null, elevated(token))).data.url
}

export async function listFolders(ws: string, id: string, parent: string | null): Promise<Folder[]> {
  return (await apiClient.get<Folder[]>(`${base(ws)}/${id}/folders`, { params: parent ? { parent } : {} })).data
}

export async function configure(ws: string, id: string, folders: Folder[]): Promise<Connection> {
  return (await apiClient.put<Connection>(`${base(ws)}/${id}`, { folders })).data
}

export async function syncNow(ws: string, id: string): Promise<Connection> {
  return (await apiClient.post<Connection>(`${base(ws)}/${id}/sync`)).data
}

export async function disconnect(ws: string, id: string, token: string): Promise<void> {
  await apiClient.delete(`${base(ws)}/${id}`, elevated(token))
}

export const ERRORS: Record<string, string> = {
  reconnect: 'Google Drive access was revoked or expired. Disconnect and connect again.',
  quota_exceeded: "The workspace reached its plan's document or storage limit. Syncing continues once there's room.",
  owner_left: 'The person who connected this drive left the workspace. Connect it again.',
  unavailable: 'Google Drive was unavailable at the last sync. It will try again.',
  denied: 'Connecting was cancelled.',
  expired: 'The connection request expired. Try again.',
  browser: 'Finish connecting in the same browser you started in.',
  failed: "Google didn't accept the connection. Try again.",
}
