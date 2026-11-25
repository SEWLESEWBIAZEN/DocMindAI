/** Sample documents API and hooks (ADR 0033). */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { useToast } from '../../components/ui'
import { apiClient } from '../../lib/api/client'
import { errorMessage } from '../../lib/api/errors'
import type { Schemas } from '../../lib/api/types'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'

export type SampleStatus = Schemas['SampleStatusResponse']

export const samplesKey = (ws: string) => ['ws', ws, 'samples'] as const

export async function getSamples(ws: string): Promise<SampleStatus> {
  return (await apiClient.get<SampleStatus>(`/workspaces/${ws}/samples`)).data
}

export async function loadSamples(ws: string): Promise<SampleStatus> {
  return (await apiClient.post<SampleStatus>(`/workspaces/${ws}/samples`)).data
}

export async function removeSamples(ws: string): Promise<number> {
  return (await apiClient.delete<{ removed: number }>(`/workspaces/${ws}/samples`)).data.removed
}

export function useSamples(enabled = true) {
  const { workspace, can } = useWorkspace()
  return useQuery({
    queryKey: samplesKey(workspace.id),
    queryFn: () => getSamples(workspace.id),
    enabled: enabled && can('documents.read'),
    staleTime: 30_000,
  })
}

/** Load or remove the samples; refreshes documents, collections and the checklist. */
export function useSampleActions() {
  const { workspace } = useWorkspace()
  const queryClient = useQueryClient()
  const toast = useToast()
  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: samplesKey(workspace.id) }),
      queryClient.invalidateQueries({ queryKey: ['ws', workspace.id, 'documents'] }),
      queryClient.invalidateQueries({ queryKey: ['ws', workspace.id, 'collections'] }),
    ])
  const load = useMutation({
    mutationFn: () => loadSamples(workspace.id),
    onSuccess: async () => {
      toast.success('Sample documents added. They will be ready to ask about in a moment.')
      await refresh()
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  const remove = useMutation({
    mutationFn: () => removeSamples(workspace.id),
    onSuccess: async (n) => {
      toast.success(n === 1 ? 'Removed 1 sample document.' : `Removed ${n} sample documents.`)
      await refresh()
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  return { load, remove }
}
