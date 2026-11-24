import { useQuery } from '@tanstack/react-query'

import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { documentKeys, listCollections } from './api'

export function useCollections() {
  const { workspace } = useWorkspace()
  return useQuery({
    queryKey: documentKeys.collections(workspace.id),
    queryFn: () => listCollections(workspace.id),
  })
}
