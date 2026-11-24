import { useQuery } from '@tanstack/react-query'
import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { useParams } from 'react-router'

import { apiClient } from '../api/client'
import type { Workspace } from '../api/types'
import { can as roleCan, type Permission } from './permissions'

export const workspacesKey = ['me', 'workspaces'] as const

export function useWorkspaces() {
  return useQuery({
    queryKey: workspacesKey,
    queryFn: async () => (await apiClient.get<Workspace[]>('/me/workspaces')).data,
  })
}

interface WorkspaceContextValue {
  workspace: Workspace
  workspaces: Workspace[]
  can: (permission: Permission) => boolean
  /** Prefix for in-workspace links, e.g. `${base}/documents`. */
  base: string
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null)

export function WorkspaceProvider({
  workspace,
  workspaces,
  children,
}: {
  workspace: Workspace
  workspaces: Workspace[]
  children: ReactNode
}) {
  const value = useMemo<WorkspaceContextValue>(
    () => ({
      workspace,
      workspaces,
      can: (permission) => roleCan(workspace.role, permission),
      base: `/w/${workspace.id}`,
    }),
    [workspace, workspaces],
  )
  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>
}

export function useWorkspace(): WorkspaceContextValue {
  const value = useContext(WorkspaceContext)
  if (!value) throw new Error('useWorkspace must be used inside a workspace route')
  return value
}

export function useWorkspaceId(): string {
  const { workspaceId } = useParams()
  if (!workspaceId) throw new Error('No workspace in the URL')
  return workspaceId
}
