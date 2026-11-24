import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import QRCode from 'qrcode'
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router'

import {
  Alert,
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Field,
  Icon,
  Loading,
  PageHeader,
  Select,
  TextInput,
  useConfirm,
  useToast,
} from '../../components/ui'
import { apiClient } from '../../lib/api/client'
import { errorMessage, isStatus } from '../../lib/api/errors'
import { setSession } from '../../lib/api/session'
import type { AuditLog, Invitation, Member, MfaSetup, MfaStatus, Role, Schemas, UsageSummary, Workspace } from '../../lib/api/types'
import { useWorkspace, workspacesKey } from '../../lib/workspace/WorkspaceContext'
import { ROLE_DESCRIPTIONS, ROLE_LABELS } from '../../lib/workspace/permissions'
import { useAuth, useUser } from '../auth/AuthProvider'
import { PasswordMeter } from '../auth/pages'
import { validatePassword } from '../auth/password'
import { formatBytes, formatDate, formatDateTime, formatNumber, formatRelative } from '../documents/format'

const ROLE_ORDER: Role[] = ['owner', 'admin', 'member', 'viewer']
const RANK: Record<Role, number> = { owner: 40, admin: 30, member: 20, viewer: 10 }

function useTitle(title: string) {
  useEffect(() => {
    document.title = `${title} · DocuMind`
  }, [title])
}

function Panel({ title, description, actions, children, id }: {
  title: string
  description?: ReactNode
  actions?: ReactNode
  children: ReactNode
  id?: string
}) {
  const headingId = `${id ?? title.toLowerCase().replace(/\W+/g, '-')}-heading`
  return (
    <section className="panel" aria-labelledby={headingId} id={id}>
      <header className="panel__header">
        <div>
          <h2 id={headingId}>{title}</h2>
          {description && <p className="panel__description">{description}</p>}
        </div>
        {actions}
      </header>
      <div className="panel__body">{children}</div>
    </section>
  )
}

// ----------------------------------------------------------------- members

export function MembersPage() {
  useTitle('Members')
  const { workspace, can, base } = useWorkspace()
  const user = useUser()
  const queryClient = useQueryClient()
  const toast = useToast()
  const confirm = useConfirm()
  const ws = workspace.id
  const members = useQuery({
    queryKey: ['ws', ws, 'members'],
    queryFn: async () => (await apiClient.get<Member[]>(`/workspaces/${ws}/members`)).data,
    enabled: workspace.kind === 'organization',
  })
  const invitations = useQuery({
    queryKey: ['ws', ws, 'invitations'],
    queryFn: async () => (await apiClient.get<Invitation[]>(`/workspaces/${ws}/invitations`)).data,
    enabled: workspace.kind === 'organization' && can('members.invite'),
  })
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<Role>('member')
  const invite = useMutation({
    mutationFn: async () => apiClient.post(`/workspaces/${ws}/invitations`, { email: email.trim(), role }),
    onSuccess: async () => {
      toast.success(`Invitation sent to ${email.trim()}.`)
      setEmail('')
      await queryClient.invalidateQueries({ queryKey: ['ws', ws, 'invitations'] })
    },
  })

  if (workspace.kind === 'personal') {
    return (
      <>
        <PageHeader title="Members" />
        <EmptyState
          icon="users"
          title="Personal workspaces have one member"
          description="To work with colleagues, create an organization and invite them."
          action={<Link className="btn btn--primary btn--md" to={`${base}/organization?create=1`}>Create organization</Link>}
        />
      </>
    )
  }

  const assignable = ROLE_ORDER.filter((r) => RANK[r] <= RANK[workspace.role])
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['ws', ws, 'members'] })

  const changeRole = async (member: Member, next: Role) => {
    try {
      await apiClient.patch(`/workspaces/${ws}/members/${member.id}`, { role: next })
      await refresh()
      toast.success(`${member.display_name} is now ${ROLE_LABELS[next].toLowerCase()}.`)
    } catch (err) {
      toast.error(errorMessage(err))
      await refresh()
    }
  }

  const remove = async (member: Member) => {
    const ok = await confirm({
      title: `Remove ${member.display_name}?`,
      description: `${member.email} loses access to ${workspace.name} immediately. Their uploaded documents stay.`,
      confirmLabel: 'Remove member',
      danger: true,
    })
    if (!ok) return
    try {
      await apiClient.delete(`/workspaces/${ws}/members/${member.id}`)
      await refresh()
      toast.success('Member removed.')
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  const revoke = async (invitation: Invitation) => {
    try {
      await apiClient.delete(`/workspaces/${ws}/invitations/${invitation.id}`)
      await queryClient.invalidateQueries({ queryKey: ['ws', ws, 'invitations'] })
      toast.success('Invitation revoked.')
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  return (
    <>
      <PageHeader title="Members" description={`People with access to ${workspace.name}.`} />
      <div className="stack" style={{ gap: 20 }}>
        {can('members.invite') && (
          <Panel title="Invite people" description="They get an email with a link that works for 7 days.">
            <form
              className="toolbar"
              style={{ marginBottom: 0 }}
              onSubmit={(e: FormEvent) => {
                e.preventDefault()
                invite.mutate()
              }}
            >
              <div className="toolbar__grow">
                <Field label="Email address">
                  <TextInput type="email" value={email} onChange={(e) => setEmail(e.target.value)} required placeholder="colleague@company.com" />
                </Field>
              </div>
              <Field label="Role">
                <Select value={role} onChange={(e) => setRole(e.target.value as Role)}>
                  {assignable.map((r) => (
                    <option key={r} value={r}>
                      {ROLE_LABELS[r]}
                    </option>
                  ))}
                </Select>
              </Field>
              <Button type="submit" variant="primary" icon="mail" loading={invite.isPending} disabled={!email.trim()}>
                Send invitation
              </Button>
            </form>
            <p className="field__hint" style={{ marginTop: 8 }}>
              {ROLE_LABELS[role]}: {ROLE_DESCRIPTIONS[role]}.
            </p>
            {invite.error && (
              <div style={{ marginTop: 12 }}>
                <Alert>{errorMessage(invite.error)}</Alert>
              </div>
            )}
          </Panel>
        )}

        <section aria-labelledby="members-heading" className="stack stack--sm">
          <h2 id="members-heading" className="section-title">
            Members {members.data && `(${members.data.length})`}
          </h2>
          {members.isLoading ? (
            <Loading lines={4} />
          ) : members.error ? (
            <ErrorState error={members.error} onRetry={() => void members.refetch()} />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Name</th>
                    <th scope="col">Role</th>
                    <th scope="col">Joined</th>
                    <th scope="col">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {members.data?.map((m) => {
                    const self = m.user_id === user.id
                    const editable = can('members.manage') && !self && RANK[m.role] <= RANK[workspace.role]
                    return (
                      <tr key={m.id}>
                        <td>
                          <div className="stack stack--sm" style={{ gap: 2 }}>
                            <strong>
                              {m.display_name} {self && <Badge>You</Badge>}
                            </strong>
                            <span className="table__sub">{m.email}</span>
                          </div>
                        </td>
                        <td>
                          {editable ? (
                            <Select
                              aria-label={`Role for ${m.display_name}`}
                              value={m.role}
                              onChange={(e) => void changeRole(m, e.target.value as Role)}
                              style={{ width: 130 }}
                            >
                              {assignable.map((r) => (
                                <option key={r} value={r}>
                                  {ROLE_LABELS[r]}
                                </option>
                              ))}
                            </Select>
                          ) : (
                            <Badge tone={m.role === 'owner' ? 'accent' : 'neutral'}>{ROLE_LABELS[m.role]}</Badge>
                          )}
                        </td>
                        <td>{formatDate(m.joined_at)}</td>
                        <td className="table__actions">
                          {editable && (
                            <Button size="sm" variant="ghost" icon="trash" label={`Remove ${m.display_name}`} onClick={() => void remove(m)} />
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {can('members.invite') && (
          <section aria-labelledby="invites-heading" className="stack stack--sm">
            <h2 id="invites-heading" className="section-title">
              Pending invitations
            </h2>
            {invitations.isLoading ? (
              <Loading lines={2} />
            ) : !invitations.data?.filter((i) => i.status === 'pending').length ? (
              <p className="muted">No pending invitations.</p>
            ) : (
              <div className="panel">
                <ul className="list">
                  {invitations.data
                    .filter((i) => i.status === 'pending')
                    .map((i) => (
                      <li key={i.id} className="list-row">
                        <Icon name="mail" />
                        <div className="list-row__main">
                          <span className="list-row__title">{i.email}</span>
                          <span className="list-row__meta">
                            {ROLE_LABELS[i.role]} · expires {formatRelative(i.expires_at)}
                          </span>
                        </div>
                        <Button size="sm" onClick={() => void revoke(i)}>
                          Revoke
                        </Button>
                      </li>
                    ))}
                </ul>
              </div>
            )}
          </section>
        )}
      </div>
    </>
  )
}

// ------------------------------------------------------------ organization

export function OrganizationPage() {
  useTitle('Organizations')
  const { workspace, workspaces } = useWorkspace()
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()
  const [creating, setCreating] = useState(params.get('create') === '1')
  const [name, setName] = useState('')
  const create = useMutation({
    mutationFn: async () => (await apiClient.post<Workspace>('/organizations', { name: name.trim() })).data,
    onSuccess: async (org) => {
      await queryClient.invalidateQueries({ queryKey: workspacesKey })
      toast.success(`${org.name} created. Invite your team next.`)
      navigate(`/w/${org.id}/members`)
    },
  })
  const orgs = workspaces.filter((w) => w.kind === 'organization')

  return (
    <>
      <PageHeader
        title="Organizations"
        description="Shared workspaces for teams: documents, collections and members with roles."
        actions={!creating && <Button variant="primary" icon="plus" onClick={() => setCreating(true)}>Create organization</Button>}
      />
      <div className="stack" style={{ gap: 20 }}>
        {creating && (
          <Panel title="Create an organization" description="You'll be its owner. You can invite members right after.">
            <form
              className="form"
              onSubmit={(e) => {
                e.preventDefault()
                create.mutate()
              }}
            >
              {create.error && <Alert>{errorMessage(create.error)}</Alert>}
              <Field label="Organization name">
                <TextInput value={name} onChange={(e) => setName(e.target.value)} maxLength={200} autoFocus placeholder="e.g. Acme Legal" />
              </Field>
              <div className="form__actions">
                <Button onClick={() => setCreating(false)}>Cancel</Button>
                <Button type="submit" variant="primary" loading={create.isPending} disabled={!name.trim()}>
                  Create organization
                </Button>
              </div>
            </form>
          </Panel>
        )}
        {orgs.length === 0 ? (
          !creating && (
            <EmptyState
              icon="building"
              title="You're not in any organization yet"
              description="Create one for your team, or accept an invitation from your profile."
            />
          )
        ) : (
          <div className="card-grid">
            {orgs.map((o) => (
              <Link key={o.id} to={`/w/${o.id}/dashboard`} className="card-link" aria-current={o.id === workspace.id ? 'page' : undefined}>
                <span className="ws-switcher__avatar" aria-hidden="true">
                  {o.name.slice(0, 1).toUpperCase()}
                </span>
                <h2>{o.name}</h2>
                <span className="muted" style={{ fontSize: '0.84rem' }}>
                  {ROLE_LABELS[o.role]}
                  {o.organization_slug ? ` · ${o.organization_slug}` : ''}
                  {o.id === workspace.id ? ' · current' : ''}
                </span>
              </Link>
            ))}
          </div>
        )}
      </div>
    </>
  )
}

// ---------------------------------------------------------------- settings

export function SettingsPage() {
  useTitle('Settings')
  const { workspace, can, workspaces } = useWorkspace()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const toast = useToast()
  const confirm = useConfirm()
  const [name, setName] = useState(workspace.name)
  const [syncedName, setSyncedName] = useState(workspace.name)
  if (syncedName !== workspace.name) {
    setSyncedName(workspace.name)
    setName(workspace.name)
  }
  const rename = useMutation({
    mutationFn: async () => apiClient.patch(`/workspaces/${workspace.id}`, { name: name.trim() }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: workspacesKey })
      toast.success('Workspace renamed.')
    },
  })

  const leave = async () => {
    const ok = await confirm({
      title: `Leave ${workspace.name}?`,
      description: "You'll lose access to its documents and your conversations there. An admin can invite you again.",
      confirmLabel: 'Leave workspace',
      danger: true,
    })
    if (!ok) return
    try {
      await apiClient.post(`/workspaces/${workspace.id}/leave`)
      await queryClient.invalidateQueries({ queryKey: workspacesKey })
      toast.success(`You left ${workspace.name}.`)
      const personal = workspaces.find((w) => w.kind === 'personal')
      navigate(personal ? `/w/${personal.id}/dashboard` : '/app', { replace: true })
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  return (
    <>
      <PageHeader title="Settings" description={`Settings for ${workspace.name}.`} />
      <div className="stack" style={{ gap: 20 }}>
        <Panel title="General">
          {can('workspace.update') ? (
            <form
              className="form"
              onSubmit={(e) => {
                e.preventDefault()
                rename.mutate()
              }}
            >
              {rename.error && <Alert>{errorMessage(rename.error)}</Alert>}
              <Field label="Workspace name">
                <TextInput value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
              </Field>
              <div className="form__actions">
                <Button type="submit" variant="primary" loading={rename.isPending} disabled={!name.trim() || name.trim() === workspace.name}>
                  Save
                </Button>
              </div>
            </form>
          ) : (
            <dl className="meta-list">
              <dt>Name</dt>
              <dd>{workspace.name}</dd>
              <dt>Your role</dt>
              <dd>{ROLE_LABELS[workspace.role]}</dd>
            </dl>
          )}
        </Panel>
        {can('audit.read') && <AuditLogPanel />}
        {workspace.kind === 'organization' && (
          <Panel title="Leave workspace" description="Owners must hand over ownership before leaving.">
            <Button variant="danger" icon="logout" onClick={() => void leave()}>
              Leave {workspace.name}
            </Button>
          </Panel>
        )}
      </div>
    </>
  )
}

const ACTION_LABELS: Record<string, string> = {
  'document.uploaded': 'Uploaded a document',
  'document.deleted': 'Deleted a document',
  'document.archived': 'Archived a document',
  'document.restored': 'Restored a document',
  'member.role_changed': 'Changed a member role',
  'member.removed': 'Removed a member',
  'invitation.created': 'Invited someone',
  'invitation.accepted': 'Accepted an invitation',
  'conversation.deleted': 'Deleted a conversation',
  'collection.created': 'Created a collection',
}

function AuditLogPanel() {
  const { workspace } = useWorkspace()
  const query = useInfiniteQuery({
    queryKey: ['ws', workspace.id, 'audit'],
    initialPageParam: undefined as number | undefined,
    queryFn: async ({ pageParam }) =>
      (await apiClient.get<AuditLog[]>(`/workspaces/${workspace.id}/audit-logs`, { params: { limit: 25, before_id: pageParam } })).data,
    getNextPageParam: (last) => (last.length === 25 ? last[last.length - 1]?.id : undefined),
  })
  const entries = query.data?.pages.flat() ?? []
  return (
    <Panel title="Audit log" description="Security-relevant actions in this workspace, newest first.">
      {query.isLoading ? (
        <Loading lines={4} />
      ) : query.error ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : entries.length === 0 ? (
        <p className="muted">No events yet.</p>
      ) : (
        <div className="stack stack--sm">
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Event</th>
                  <th scope="col">Target</th>
                  <th scope="col">IP</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => (
                  <tr key={e.id}>
                    <td title={formatDateTime(e.created_at)}>{formatRelative(e.created_at)}</td>
                    <td>
                      {ACTION_LABELS[e.action] ?? e.action}
                      <div className="table__sub">{e.action}</div>
                    </td>
                    <td className="table__sub">{e.target_type ?? '—'}</td>
                    <td className="table__sub">{e.ip_address ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {query.hasNextPage && (
            <Button size="sm" loading={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>
              Load more
            </Button>
          )}
        </div>
      )}
    </Panel>
  )
}

// ----------------------------------------------------------------- profile

export function ProfilePage() {
  useTitle('Profile')
  const location = useLocation()
  useEffect(() => {
    if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView()
  }, [location.hash])
  return (
    <>
      <PageHeader title="Profile" description="Your account, sign-in and security. These apply in every workspace." />
      <div className="stack" style={{ gap: 20 }}>
        <ProfileDetails />
        <MyInvitations />
        <PasswordPanel />
        <SecurityPanel />
        <SessionsPanel />
      </div>
    </>
  )
}

function ProfileDetails() {
  const user = useUser()
  const { setUser } = useAuth()
  const toast = useToast()
  const [name, setName] = useState(user.display_name)
  const save = useMutation({
    mutationFn: async () => (await apiClient.patch<Schemas['UserResponse']>('/me', { display_name: name.trim() })).data,
    onSuccess: (updated) => {
      setUser(updated)
      toast.success('Profile updated.')
    },
  })
  return (
    <Panel title="Profile">
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault()
          save.mutate()
        }}
      >
        {save.error && <Alert>{errorMessage(save.error)}</Alert>}
        <div className="grid-2">
          <Field label="Name">
            <TextInput value={name} onChange={(e) => setName(e.target.value)} maxLength={200} autoComplete="name" />
          </Field>
          <Field label="Email" hint={user.email_verified ? 'Verified' : 'Not verified'}>
            <TextInput value={user.email} readOnly />
          </Field>
        </div>
        <div className="form__actions">
          <Button type="submit" variant="primary" loading={save.isPending} disabled={!name.trim() || name.trim() === user.display_name}>
            Save
          </Button>
        </div>
      </form>
    </Panel>
  )
}

function MyInvitations() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const toast = useToast()
  const { data } = useQuery({
    queryKey: ['me', 'invitations'],
    queryFn: async () => (await apiClient.get<Invitation[]>('/me/invitations')).data,
  })
  const pending = (data ?? []).filter((i) => i.status === 'pending')
  if (!pending.length) return null
  const respond = async (invitation: Invitation, accept: boolean) => {
    try {
      if (accept) {
        const { data: accepted } = await apiClient.post<Schemas['AcceptedInvitationResponse']>(`/me/invitations/${invitation.id}/accept`)
        await queryClient.invalidateQueries({ queryKey: workspacesKey })
        toast.success(`You joined ${invitation.workspace_name}.`)
        navigate(`/w/${accepted.workspace_id}/dashboard`)
      } else {
        await apiClient.post(`/me/invitations/${invitation.id}/decline`)
        toast.success('Invitation declined.')
      }
      await queryClient.invalidateQueries({ queryKey: ['me', 'invitations'] })
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }
  return (
    <Panel title="Invitations" description="Workspaces you've been invited to.">
      <ul className="list">
        {pending.map((i) => (
          <li key={i.id} className="list-row" style={{ paddingInline: 0 }}>
            <Icon name="building" />
            <div className="list-row__main">
              <span className="list-row__title">{i.workspace_name}</span>
              <span className="list-row__meta">
                As {ROLE_LABELS[i.role].toLowerCase()} · expires {formatRelative(i.expires_at)}
              </span>
            </div>
            <Button size="sm" onClick={() => void respond(i, false)}>
              Decline
            </Button>
            <Button size="sm" variant="primary" onClick={() => void respond(i, true)}>
              Accept
            </Button>
          </li>
        ))}
      </ul>
    </Panel>
  )
}

function PasswordPanel() {
  const toast = useToast()
  const { setUser } = useAuth()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const problem = validatePassword(next)
    if (problem) {
      setError(problem)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const { data } = await apiClient.post<Schemas['TokenResponse']>(
        '/me/password',
        { current_password: current, new_password: next },
        { withCredentials: true },
      )
      setSession(data)
      setUser(data.user)
      setCurrent('')
      setNext('')
      toast.success('Password changed. Other devices were signed out.')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Panel title="Password" description="Changing it signs you out on your other devices.">
      <form className="form" onSubmit={submit}>
        {error && <Alert>{error}</Alert>}
        <div className="grid-2">
          <Field label="Current password">
            <TextInput type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
          </Field>
          <div className="stack stack--sm">
            <Field label="New password" hint="At least 10 characters.">
              <TextInput type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
            </Field>
            <PasswordMeter password={next} />
          </div>
        </div>
        <div className="form__actions">
          <Button type="submit" variant="primary" loading={busy} disabled={!current || !next}>
            Change password
          </Button>
        </div>
      </form>
    </Panel>
  )
}

function SecurityPanel() {
  const { refreshUser } = useAuth()
  const toast = useToast()
  const queryClient = useQueryClient()
  const status = useQuery({
    queryKey: ['me', 'mfa'],
    queryFn: async () => (await apiClient.get<MfaStatus>('/me/mfa')).data,
  })
  const [mode, setMode] = useState<'idle' | 'setup' | 'disable' | 'codes'>('idle')
  const [password, setPassword] = useState('')
  const [setup, setSetup] = useState<MfaSetup | null>(null)
  const [qrImage, setQrImage] = useState<{ uri: string; data: string } | null>(null)
  const qr = setup && qrImage?.uri === setup.otpauth_uri ? qrImage.data : null
  const [code, setCode] = useState('')
  const [codes, setCodes] = useState<string[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!setup) return
    const uri = setup.otpauth_uri
    QRCode.toDataURL(uri, { margin: 1, width: 192 })
      .then((data) => setQrImage({ uri, data }))
      .catch(() => undefined)
  }, [setup])

  const reset = () => {
    setMode('idle')
    setPassword('')
    setCode('')
    setSetup(null)
    setError(null)
  }
  const done = async () => {
    await queryClient.invalidateQueries({ queryKey: ['me', 'mfa'] })
    await refreshUser()
  }
  const call = async (action: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const start = () =>
    call(async () => {
      const { data } = await apiClient.post<MfaSetup>('/me/mfa/totp/setup', { password })
      setSetup(data)
      setPassword('')
    })
  const confirmSetup = () =>
    call(async () => {
      const { data } = await apiClient.post<Schemas['RecoveryCodesResponse']>('/me/mfa/totp/confirm', { code: code.replace(/\s/g, '') })
      setCodes(data.recovery_codes)
      setSetup(null)
      setCode('')
      setMode('codes')
      await done()
      toast.success('Two-factor authentication is on.')
    })
  const disable = () =>
    call(async () => {
      const recovery = code.trim().length > 8
      await apiClient.post('/me/mfa/disable', {
        password,
        ...(recovery ? { recovery_code: code.trim() } : { code: code.replace(/\s/g, '') }),
      })
      reset()
      await done()
      toast.success('Two-factor authentication is off.')
    })
  const regenerateCodes = () =>
    call(async () => {
      const { data } = await apiClient.post<Schemas['RecoveryCodesResponse']>('/me/mfa/recovery-codes', { code: code.replace(/\s/g, '') })
      setCodes(data.recovery_codes)
      setCode('')
      setMode('codes')
      await done()
    })

  const enabled = status.data?.enabled ?? false
  return (
    <Panel
      id="security"
      title="Two-factor authentication"
      description="A code from an authenticator app at sensitive moments, such as permanently deleting documents."
      actions={status.data && <Badge tone={enabled ? 'success' : 'warning'}>{enabled ? 'On' : 'Off'}</Badge>}
    >
      <div className="stack">
        {error && <Alert>{error}</Alert>}
        {status.isLoading && <Loading lines={2} />}

        {mode === 'codes' && codes && (
          <div className="stack">
            <Alert tone="warning">
              Save these recovery codes somewhere safe. Each works once if you lose your device. They won't be shown again.
            </Alert>
            <ul className="codes" aria-label="Recovery codes">
              {codes.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
            <div className="row">
              <Button icon="copy" onClick={() => void navigator.clipboard.writeText(codes.join('\n')).then(() => toast.success('Codes copied.'))}>
                Copy codes
              </Button>
              <Button variant="primary" onClick={() => { setCodes(null); reset() }}>
                I've saved them
              </Button>
            </div>
          </div>
        )}

        {mode === 'idle' && status.data && (
          <div className="row">
            {enabled ? (
              <>
                <span className="muted">
                  On since {formatDate(status.data.enabled_at)} · {status.data.recovery_codes_remaining} recovery codes left
                </span>
                <Button icon="key" onClick={() => { setMode('disable'); setCodes(null) }}>
                  Manage
                </Button>
              </>
            ) : (
              <Button variant="primary" icon="shield" onClick={() => setMode('setup')}>
                Set up 2FA
              </Button>
            )}
          </div>
        )}

        {mode === 'setup' && !setup && (
          <form className="form" onSubmit={(e) => { e.preventDefault(); void start() }}>
            <Field label="Confirm your password to continue">
              <TextInput type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />
            </Field>
            <div className="form__actions">
              <Button onClick={reset}>Cancel</Button>
              <Button type="submit" variant="primary" loading={busy} disabled={!password}>
                Continue
              </Button>
            </div>
          </form>
        )}

        {mode === 'setup' && setup && (
          <form className="form" onSubmit={(e) => { e.preventDefault(); void confirmSetup() }}>
            <ol className="stack stack--sm" style={{ paddingLeft: 18, margin: 0 }}>
              <li>Open an authenticator app (1Password, Google Authenticator, Microsoft Authenticator…).</li>
              <li>Scan the QR code, or type the setup key.</li>
              <li>Enter the 6-digit code it shows.</li>
            </ol>
            <div className="row" style={{ alignItems: 'flex-start', gap: 20 }}>
              {qr && (
                <div className="qr">
                  <img src={qr} width={192} height={192} alt="QR code for your authenticator app. If you can't scan it, use the setup key." />
                </div>
              )}
              <div className="stack stack--sm" style={{ flex: 1, minWidth: 220 }}>
                <span className="field__label">Setup key</span>
                <code className="secret" aria-label="Setup key">
                  {setup.secret.replace(/(.{4})/g, '$1 ').trim()}
                </code>
                <Field label="Code from your app">
                  <TextInput inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value)} maxLength={8} autoFocus />
                </Field>
              </div>
            </div>
            <div className="form__actions">
              <Button onClick={reset}>Cancel</Button>
              <Button type="submit" variant="primary" loading={busy} disabled={code.replace(/\s/g, '').length < 6}>
                Turn on 2FA
              </Button>
            </div>
          </form>
        )}

        {mode === 'disable' && (
          <form className="form" onSubmit={(e) => { e.preventDefault(); void disable() }}>
            <p className="muted">
              Enter a code to get new recovery codes, or your password and a code to turn 2FA off.
            </p>
            <div className="grid-2">
              <Field label="Authentication or recovery code">
                <TextInput value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" autoFocus />
              </Field>
              <Field label="Password" hint="Only needed to turn 2FA off.">
                <TextInput type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
              </Field>
            </div>
            <div className="form__actions">
              <Button onClick={reset}>Cancel</Button>
              <Button loading={busy} disabled={code.trim().length < 6} onClick={() => void regenerateCodes()}>
                New recovery codes
              </Button>
              <Button type="submit" variant="danger" loading={busy} disabled={!password || code.trim().length < 6}>
                Turn off 2FA
              </Button>
            </div>
          </form>
        )}
      </div>
    </Panel>
  )
}

function SessionsPanel() {
  const { signOut } = useAuth()
  const confirm = useConfirm()
  const navigate = useNavigate()
  return (
    <Panel title="Sessions" description="Sign out of DocuMind on every browser and device, including this one.">
      <Button
        icon="logout"
        onClick={async () => {
          const ok = await confirm({
            title: 'Sign out everywhere?',
            description: 'Every session ends, including this one. You will need to sign in again.',
            confirmLabel: 'Sign out everywhere',
            danger: true,
          })
          if (ok) {
            await signOut({ everywhere: true })
            navigate('/login', { replace: true })
          }
        }}
      >
        Sign out everywhere
      </Button>
    </Panel>
  )
}

// ------------------------------------------------------------------- usage

export function UsagePage() {
  useTitle('Usage')
  const { workspace, can } = useWorkspace()
  const [days, setDays] = useState(30)
  const query = useQuery({
    queryKey: ['ws', workspace.id, 'usage', days],
    queryFn: async () => (await apiClient.get<UsageSummary>(`/workspaces/${workspace.id}/usage`, { params: { days } })).data,
    enabled: can('usage.read'),
  })

  const header = (
    <PageHeader
      title="Usage"
      description="Documents, processing and AI activity in this workspace."
      actions={
        <Field label="Period" hideLabel>
          <Select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Period">
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </Select>
        </Field>
      }
    />
  )
  if (!can('usage.read') || isStatus(query.error, 403)) {
    return (
      <>
        <PageHeader title="Usage" />
        <EmptyState icon="lock" title="Only admins can see usage" description="Ask an admin or owner of this workspace." />
      </>
    )
  }
  if (query.isLoading) return <>{header}<Loading label="Loading usage" lines={6} /></>
  if (query.error || !query.data) return <>{header}<ErrorState error={query.error} onRetry={() => void query.refetch()} /></>
  const u = query.data
  const tokens = u.ai.llm_input_tokens + u.ai.llm_output_tokens

  return (
    <>
      {header}
      <div className="stack" style={{ gap: 24 }}>
        <section aria-labelledby="usage-docs" className="stack stack--sm">
          <h2 id="usage-docs" className="section-title">
            Documents (now)
          </h2>
          <div className="stat-grid">
            <Stat label="Documents" value={formatNumber(u.documents.total)} hint={`${u.documents.ready} ready · ${u.documents.archived} archived`} />
            <Stat label="Storage" value={formatBytes(u.documents.storage_bytes)} hint="all versions" />
            <Stat label="Pages" value={formatNumber(u.documents.pages)} hint={`${formatNumber(u.documents.chunks)} indexed passages`} />
            <Stat label="Failed" value={formatNumber(u.documents.failed)} hint={`${u.documents.processing} in progress`} />
          </div>
        </section>
        <section aria-labelledby="usage-ai" className="stack stack--sm">
          <h2 id="usage-ai" className="section-title">
            AI and processing (last {days} days)
          </h2>
          <div className="stat-grid">
            <Stat label="Questions" value={formatNumber(u.ai.questions)} hint={`${u.ai.conversations} conversations in total`} />
            <Stat
              label="Answers"
              value={formatNumber(u.ai.answers_completed)}
              hint={`${u.ai.answers_failed} failed · ${u.ai.answers_cancelled} stopped`}
            />
            <Stat label="LLM tokens" value={formatNumber(tokens)} hint={`${formatNumber(u.ai.llm_input_tokens)} in · ${formatNumber(u.ai.llm_output_tokens)} out`} />
            <Stat
              label="Embedding tokens"
              value={formatNumber(u.processing.embedding_tokens + u.ai.query_embedding_tokens)}
              hint={`${u.processing.jobs_succeeded} documents processed · ${u.processing.jobs_failed} failed`}
            />
          </div>
        </section>
        <Panel title="Questions per day" description={`${u.daily[0]?.day ?? ''} to ${u.daily[u.daily.length - 1]?.day ?? ''} (UTC)`}>
          <DailyChart daily={u.daily} />
        </Panel>
        {u.members.length > 0 && (
          <Panel title="By member" description="Questions and LLM tokens in the period.">
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Member</th>
                    <th scope="col">Questions</th>
                    <th scope="col">LLM tokens</th>
                  </tr>
                </thead>
                <tbody>
                  {u.members.map((m) => (
                    <tr key={m.user_id}>
                      <td>{m.display_name}</td>
                      <td>{formatNumber(m.questions)}</td>
                      <td>{formatNumber(m.llm_tokens)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        )}
        <p className="muted" style={{ fontSize: '0.82rem' }}>
          Figures are computed from activity records. One-off answers from the API's /ask endpoint aren't counted yet.
          Plans and limits arrive with billing.
        </p>
      </div>
    </>
  )
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="stat">
      <span className="stat__label">{label}</span>
      <span className="stat__value">{value}</span>
      {hint && <span className="stat__hint">{hint}</span>}
    </div>
  )
}

function DailyChart({ daily }: { daily: UsageSummary['daily'] }) {
  const max = useMemo(() => Math.max(1, ...daily.map((d) => d.questions)), [daily])
  const total = daily.reduce((sum, d) => sum + d.questions, 0)
  return (
    <figure style={{ margin: 0 }}>
      <div className="bar-chart" role="img" aria-label={`Bar chart: ${total} questions over ${daily.length} days, at most ${max} in one day.`}>
        {daily.map((d) => (
          <div
            key={d.day}
            className={`bar-chart__bar${d.questions === 0 ? ' bar-chart__bar--zero' : ''}`}
            style={{ height: `${(d.questions / max) * 100}%` }}
            title={`${d.day}: ${d.questions} questions, ${d.documents_processed} documents processed`}
          />
        ))}
      </div>
      <div className="bar-chart__axis" aria-hidden="true">
        <span>{daily[0]?.day}</span>
        <span>{daily[daily.length - 1]?.day}</span>
      </div>
      <details style={{ marginTop: 12 }}>
        <summary className="muted" style={{ cursor: 'pointer', fontSize: '0.85rem' }}>
          Show as table
        </summary>
        <div className="table-wrap" style={{ marginTop: 8, maxHeight: 280, overflowY: 'auto' }}>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Day</th>
                <th scope="col">Questions</th>
                <th scope="col">Tokens in</th>
                <th scope="col">Tokens out</th>
                <th scope="col">Documents processed</th>
              </tr>
            </thead>
            <tbody>
              {[...daily].reverse().map((d) => (
                <tr key={d.day}>
                  <td>{d.day}</td>
                  <td>{d.questions}</td>
                  <td>{formatNumber(d.llm_input_tokens)}</td>
                  <td>{formatNumber(d.llm_output_tokens)}</td>
                  <td>{d.documents_processed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  )
}
