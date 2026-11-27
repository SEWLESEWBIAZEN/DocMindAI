import { apiClient } from '../../lib/api/client'
import type {
  Collection,
  DocumentItem,
  DocumentPage,
  DocumentStatus,
  DocumentVersion,
  ExtractedPage,
  ProcessingStatus,
  Schemas,
} from '../../lib/api/types'

export const SUPPORTED_EXTENSIONS = ['.pdf', '.docx', '.txt', '.md', '.markdown']
export const ACCEPT = SUPPORTED_EXTENSIONS.join(',')

export interface DocumentFilters {
  q?: string
  status?: DocumentStatus | ''
  archived?: 'exclude' | 'include' | 'only'
  collectionId?: string
  format?: string
  limit?: number
  offset?: number
}

export const documentKeys = {
  all: (ws: string) => ['ws', ws, 'documents'] as const,
  list: (ws: string, filters: DocumentFilters) => ['ws', ws, 'documents', 'list', filters] as const,
  detail: (ws: string, id: string) => ['ws', ws, 'documents', 'detail', id] as const,
  pages: (ws: string, id: string) => ['ws', ws, 'documents', 'pages', id] as const,
  processing: (ws: string, id: string) => ['ws', ws, 'documents', 'processing', id] as const,
  versions: (ws: string, id: string) => ['ws', ws, 'documents', 'versions', id] as const,
  collections: (ws: string) => ['ws', ws, 'collections'] as const,
}

const docs = (ws: string) => `/workspaces/${ws}/documents`

export async function listDocuments(ws: string, f: DocumentFilters): Promise<DocumentPage> {
  const { data } = await apiClient.get<DocumentPage>(docs(ws), {
    params: {
      q: f.q || undefined,
      status: f.status || undefined,
      archived: f.archived ?? 'exclude',
      collection_id: f.collectionId || undefined,
      format: f.format || undefined,
      limit: f.limit ?? 25,
      offset: f.offset ?? 0,
    },
  })
  return data
}

export async function getDocument(ws: string, id: string): Promise<DocumentItem> {
  return (await apiClient.get<DocumentItem>(`${docs(ws)}/${id}`)).data
}

export async function uploadDocument(
  ws: string,
  file: File,
  options: { title?: string; collectionId?: string; onProgress?: (fraction: number) => void; signal?: AbortSignal } = {},
): Promise<DocumentItem> {
  const form = new FormData()
  form.append('file', file)
  if (options.title) form.append('title', options.title)
  if (options.collectionId) form.append('collection_id', options.collectionId)
  const { data } = await apiClient.post<DocumentItem>(docs(ws), form, {
    timeout: 0,
    signal: options.signal,
    onUploadProgress: (event) => {
      if (event.total) options.onProgress?.(event.loaded / event.total)
    },
  })
  return data
}

export async function renameDocument(ws: string, id: string, title: string): Promise<DocumentItem> {
  return (await apiClient.patch<DocumentItem>(`${docs(ws)}/${id}`, { title })).data
}

export async function archiveDocument(ws: string, id: string): Promise<DocumentItem> {
  return (await apiClient.post<DocumentItem>(`${docs(ws)}/${id}/archive`)).data
}

export async function restoreDocument(ws: string, id: string): Promise<DocumentItem> {
  return (await apiClient.post<DocumentItem>(`${docs(ws)}/${id}/restore`)).data
}

/** Permanent deletion needs a fresh 2FA step-up token (see `stepUp`). */
export async function deleteDocument(ws: string, id: string, elevatedToken: string): Promise<void> {
  await apiClient.delete(`${docs(ws)}/${id}`, { headers: { Authorization: `Bearer ${elevatedToken}` } })
}

export async function reprocessDocument(ws: string, id: string): Promise<void> {
  await apiClient.post(`${docs(ws)}/${id}/reprocess`)
}

export async function downloadUrl(ws: string, id: string): Promise<Schemas['DownloadUrlResponse']> {
  return (await apiClient.get<Schemas['DownloadUrlResponse']>(`${docs(ws)}/${id}/download-url`)).data
}

export async function listVersions(ws: string, id: string): Promise<DocumentVersion[]> {
  return (await apiClient.get<DocumentVersion[]>(`${docs(ws)}/${id}/versions`)).data
}

export async function uploadVersion(ws: string, id: string, file: File): Promise<DocumentVersion> {
  const form = new FormData()
  form.append('file', file)
  return (await apiClient.post<DocumentVersion>(`${docs(ws)}/${id}/versions`, form, { timeout: 0 })).data
}

export async function processingStatus(ws: string, id: string): Promise<ProcessingStatus> {
  return (await apiClient.get<ProcessingStatus>(`${docs(ws)}/${id}/processing`)).data
}

export async function allPages(ws: string, id: string): Promise<ExtractedPage[]> {
  const pages: ExtractedPage[] = []
  for (let offset = 0; offset < 5000; offset += 100) {
    const { data } = await apiClient.get<Schemas['PageListResponse']>(`${docs(ws)}/${id}/pages`, {
      params: { offset, limit: 100 },
    })
    pages.push(...data.items)
    if (pages.length >= data.total || data.items.length === 0) break
  }
  return pages
}

export async function listCollections(ws: string): Promise<Collection[]> {
  return (await apiClient.get<Collection[]>(`/workspaces/${ws}/collections`)).data
}

export async function createCollection(ws: string, name: string, description?: string): Promise<Collection> {
  return (
    await apiClient.post<Collection>(`/workspaces/${ws}/collections`, { name, description: description || null })
  ).data
}

export async function updateCollection(
  ws: string,
  id: string,
  body: { name?: string; description?: string | null },
): Promise<Collection> {
  return (await apiClient.patch<Collection>(`/workspaces/${ws}/collections/${id}`, body)).data
}

export async function deleteCollection(ws: string, id: string): Promise<void> {
  await apiClient.delete(`/workspaces/${ws}/collections/${id}`)
}

export async function addToCollection(ws: string, collectionId: string, documentId: string): Promise<void> {
  await apiClient.put(`/workspaces/${ws}/collections/${collectionId}/documents/${documentId}`)
}

export async function removeFromCollection(ws: string, collectionId: string, documentId: string): Promise<void> {
  await apiClient.delete(`/workspaces/${ws}/collections/${collectionId}/documents/${documentId}`)
}

/** 2FA step-up: a short-lived elevated token for one sensitive action. */
export async function stepUp(body: {
  code?: string
  recovery_code?: string
  email_code?: string
  passkey?: Record<string, unknown>
}): Promise<string> {
  const { data } = await apiClient.post<Schemas['StepUpResponse']>('/auth/mfa/verify', body)
  return data.access_token
}

/** WebAuthn request options for confirming with a passkey (ADR 0029). */
export async function passkeyOptions(): Promise<Record<string, unknown>> {
  return (await apiClient.post<Record<string, unknown>>('/auth/mfa/passkey-options')).data
}

/** Email a one-time code to the signed-in user (fallback second factor, ADR 0027). */
export async function sendEmailCode(): Promise<string> {
  const { data } = await apiClient.post<{ detail: string }>('/auth/mfa/email-code')
  return data.detail
}
