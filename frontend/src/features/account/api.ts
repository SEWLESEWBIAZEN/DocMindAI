import { apiClient } from '../../lib/api/client'
import type { Schemas } from '../../lib/api/types'

export type DataExport = Schemas['DataExportResponse']

const elevated = (token?: string) => (token ? { headers: { Authorization: `Bearer ${token}` } } : {})

export const exportsKey = ['me', 'exports'] as const

export async function changeEmail(body: { new_email: string; password: string }, token?: string): Promise<string> {
  return (await apiClient.post<{ detail: string }>('/me/email', body, elevated(token))).data.detail
}

export async function confirmEmailChange(token: string): Promise<string> {
  return (await apiClient.post<{ detail: string }>('/auth/email-change/confirm', { token }, { skipAuth: true })).data.detail
}

export async function listExports(): Promise<DataExport[]> {
  return (await apiClient.get<DataExport[]>('/me/exports')).data
}

export async function requestExport(): Promise<DataExport> {
  return (await apiClient.post<DataExport>('/me/exports')).data
}

export async function exportLink(id: string): Promise<string> {
  return (await apiClient.get<Schemas['DownloadLinkResponse']>(`/me/exports/${id}/download`)).data.url
}

export async function deleteAccount(body: { password: string; confirm: string }, token?: string): Promise<void> {
  await apiClient.delete('/me', { data: body, ...elevated(token) })
}

export async function deleteOrganization(workspaceId: string, confirmName: string, token: string): Promise<void> {
  await apiClient.delete(`/workspaces/${workspaceId}`, { data: { confirm_name: confirmName }, ...elevated(token) })
}
