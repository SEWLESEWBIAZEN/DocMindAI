import { useQueryClient } from '@tanstack/react-query'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { errorMessage } from '../../lib/api/errors'
import { SUPPORTED_EXTENSIONS, documentKeys, uploadDocument } from './api'

export type UploadState = 'queued' | 'uploading' | 'done' | 'failed'

export interface UploadItem {
  id: number
  name: string
  size: number
  state: UploadState
  progress: number
  error?: string
  documentId?: string
}

interface UploadContextValue {
  items: UploadItem[]
  enqueue: (files: File[], options?: { collectionId?: string }) => void
  clearFinished: () => void
  active: number
}

const UploadContext = createContext<UploadContextValue | null>(null)
const PARALLEL = 2

export function validateFile(file: File): string | null {
  const name = file.name.toLowerCase()
  if (!SUPPORTED_EXTENSIONS.some((ext) => name.endsWith(ext))) {
    return `Unsupported file type. Use ${SUPPORTED_EXTENSIONS.join(', ')}.`
  }
  if (file.size === 0) return 'The file is empty.'
  return null
}

/** Workspace-wide upload queue (two uploads at a time) with per-file progress. */
export function UploadProvider({ workspaceId, children }: { workspaceId: string; children: ReactNode }) {
  const queryClient = useQueryClient()
  const [items, setItems] = useState<UploadItem[]>([])
  const pending = useRef<{ item: UploadItem; file: File; collectionId?: string }[]>([])
  const running = useRef(0)
  const nextId = useRef(1)

  const patch = useCallback((id: number, change: Partial<UploadItem>) => {
    setItems((current) => current.map((item) => (item.id === id ? { ...item, ...change } : item)))
  }, [])

  // `pump` starts queued uploads; it re-runs itself as uploads finish, so the
  // implementation lives in a ref (always current) behind a stable function.
  const pumpImpl = useRef<() => void>(() => undefined)
  useEffect(() => {
    pumpImpl.current = () => {
      while (running.current < PARALLEL && pending.current.length > 0) {
        const job = pending.current.shift()
        if (!job) break
        running.current += 1
        patch(job.item.id, { state: 'uploading' })
        uploadDocument(workspaceId, job.file, {
          collectionId: job.collectionId,
          onProgress: (fraction) => patch(job.item.id, { progress: fraction }),
        })
          .then((doc) => {
            patch(job.item.id, { state: 'done', progress: 1, documentId: doc.id })
            void queryClient.invalidateQueries({ queryKey: documentKeys.all(workspaceId) })
            void queryClient.invalidateQueries({ queryKey: documentKeys.collections(workspaceId) })
          })
          .catch((error: unknown) => patch(job.item.id, { state: 'failed', error: errorMessage(error) }))
          .finally(() => {
            running.current -= 1
            pumpImpl.current()
          })
      }
    }
  })
  const pump = useCallback(() => pumpImpl.current(), [])

  const enqueue = useCallback(
    (files: File[], options?: { collectionId?: string }) => {
      const added: UploadItem[] = []
      for (const file of files) {
        const problem = validateFile(file)
        const item: UploadItem = {
          id: nextId.current++,
          name: file.name,
          size: file.size,
          state: problem ? 'failed' : 'queued',
          progress: 0,
          error: problem ?? undefined,
        }
        added.push(item)
        if (!problem) pending.current.push({ item, file, collectionId: options?.collectionId })
      }
      setItems((current) => [...current, ...added])
      pump()
    },
    [pump],
  )

  const clearFinished = useCallback(() => {
    setItems((current) => current.filter((item) => item.state === 'queued' || item.state === 'uploading'))
  }, [])

  const active = items.filter((item) => item.state === 'queued' || item.state === 'uploading').length
  const value = useMemo(() => ({ items, enqueue, clearFinished, active }), [items, enqueue, clearFinished, active])
  return <UploadContext.Provider value={value}>{children}</UploadContext.Provider>
}

export function useUploads(): UploadContextValue {
  const value = useContext(UploadContext)
  if (!value) throw new Error('useUploads must be used inside <UploadProvider>')
  return value
}
