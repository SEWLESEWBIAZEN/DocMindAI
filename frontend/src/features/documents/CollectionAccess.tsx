/** Restrict a collection to some members (ADR 0036). Owners and admins. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Badge, Button, Loading, useToast } from '../../components/ui'
import { apiClient } from '../../lib/api/client'
import { errorMessage } from '../../lib/api/errors'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'

interface Member {
  user_id: string
  display_name: string
  email: string
  role: string
}

export function CollectionAccess({ collectionId }: { collectionId: string }) {
  const { workspace } = useWorkspace()
  const toast = useToast()
  const queryClient = useQueryClient()
  const access = useQuery({
    queryKey: ['ws', workspace.id, 'collections', collectionId, 'access'],
    queryFn: async () =>
      (await apiClient.get<{ restricted: boolean; member_user_ids: string[] }>(`/workspaces/${workspace.id}/collections/${collectionId}/access`)).data,
  })
  const members = useQuery({
    queryKey: ['ws', workspace.id, 'members'],
    queryFn: async () => (await apiClient.get<Member[]>(`/workspaces/${workspace.id}/members`)).data,
  })
  const [draft, setDraft] = useState<{ restricted: boolean; ids: string[] } | null>(null)
  const value = draft ?? { restricted: access.data?.restricted ?? false, ids: access.data?.member_user_ids ?? [] }
  const save = useMutation({
    mutationFn: () =>
      apiClient.put(`/workspaces/${workspace.id}/collections/${collectionId}/access`, {
        restricted: value.restricted,
        member_user_ids: value.restricted ? value.ids : [],
      }),
    onSuccess: async () => {
      toast.success(value.restricted ? 'Only the chosen members (and admins) can see this collection now.' : 'Everyone in the workspace can see this collection.')
      setDraft(null)
      await queryClient.invalidateQueries({ queryKey: ['ws', workspace.id, 'collections'] })
      await queryClient.invalidateQueries({ queryKey: ['ws', workspace.id, 'documents'] })
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  if (access.isLoading || members.isLoading) return <Loading lines={2} />
  const regular = (members.data ?? []).filter((m) => m.role === 'member' || m.role === 'viewer')
  return (
    <section className="panel" aria-labelledby="collection-access">
      <header className="panel__header">
        <div>
          <h2 id="collection-access">
            Who can see it {access.data?.restricted && <Badge tone="warning">Restricted</Badge>}
          </h2>
          <p className="panel__description">
            Restricted collections and their documents are hidden from everyone else: in lists, search, answers, extraction and
            downloads. Owners and admins always see everything.
          </p>
        </div>
      </header>
      <div className="panel__body stack">
        <div className="choice-row" role="radiogroup" aria-label="Who can see this collection">
          <label className={`choice${!value.restricted ? ' choice--selected' : ''}`}>
            <input type="radio" name="access" checked={!value.restricted} onChange={() => setDraft({ ...value, restricted: false })} />
            <span>Everyone in the workspace</span>
          </label>
          <label className={`choice${value.restricted ? ' choice--selected' : ''}`}>
            <input type="radio" name="access" checked={value.restricted} onChange={() => setDraft({ ...value, restricted: true })} />
            <span>Only chosen members</span>
          </label>
        </div>
        {value.restricted && (
          <ul className="doc-picker" aria-label="Members who can see it">
            {regular.length === 0 && <li className="muted">There are no members or viewers to choose yet.</li>}
            {regular.map((m) => (
              <li key={m.user_id}>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={value.ids.includes(m.user_id)}
                    onChange={(e) =>
                      setDraft({ ...value, ids: e.target.checked ? [...value.ids, m.user_id] : value.ids.filter((x) => x !== m.user_id) })
                    }
                  />
                  {m.display_name} <span className="muted">({m.email})</span>
                </label>
              </li>
            ))}
          </ul>
        )}
        <div>
          <Button variant="primary" loading={save.isPending} disabled={draft === null} onClick={() => save.mutate()}>
            Save access
          </Button>
        </div>
      </div>
    </section>
  )
}
