/** Account self-service (ADR 0039): email change, "download my data", deleting an account or organization. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent, type ReactNode } from 'react'
import { useNavigate } from 'react-router'

import { Alert, Badge, Button, Dialog, Field, TextInput, useToast } from '../../components/ui'
import { ApiError, errorMessage } from '../../lib/api/errors'
import { useWorkspace, workspacesKey } from '../../lib/workspace/WorkspaceContext'
import { useAuth, useUser } from '../auth/AuthProvider'
import { StepUpDialog } from '../documents/StepUpDialog'
import { formatBytes, formatDateTime } from '../documents/format'
import { changeEmail, deleteAccount, deleteOrganization, exportLink, exportsKey, listExports, requestExport } from './api'

function Section({ id, title, description, children, danger }: {
  id: string
  title: string
  description: ReactNode
  children: ReactNode
  danger?: boolean
}) {
  return (
    <section className={`panel${danger ? ' panel--danger' : ''}`} aria-labelledby={`${id}-heading`} id={id}>
      <header className="panel__header">
        <div>
          <h2 id={`${id}-heading`}>{title}</h2>
          <p className="panel__description">{description}</p>
        </div>
      </header>
      <div className="panel__body">{children}</div>
    </section>
  )
}

const needsStepUp = (err: unknown) => err instanceof ApiError && err.code === 'mfa_step_up_required'

// ---------------------------------------------------------------- email

export function EmailPanel() {
  const user = useUser()
  const toast = useToast()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [sent, setSent] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [stepUp, setStepUp] = useState(false)

  const send = async (token?: string) => {
    setBusy(true)
    setError(null)
    try {
      setSent(await changeEmail({ new_email: email.trim(), password }, token))
      setPassword('')
      toast.success('Check your new inbox.')
    } catch (err) {
      if (needsStepUp(err)) setStepUp(true)
      else setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section
      id="email"
      title="Email"
      description={
        <>
          You sign in with <strong>{user.email}</strong>. To change it, we send a link to the new address; nothing changes
          until you open it.
        </>
      }
    >
      <form
        className="form"
        onSubmit={(e: FormEvent) => {
          e.preventDefault()
          void send()
        }}
      >
        {error && <Alert>{error}</Alert>}
        {sent && <Alert tone="success">{sent}</Alert>}
        <div className="grid-2">
          <Field label="New email">
            <TextInput type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </Field>
          <Field label="Current password">
            <TextInput
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </Field>
        </div>
        <div className="form__actions">
          <Button type="submit" variant="primary" loading={busy} disabled={!email.trim() || !password}>
            Send confirmation link
          </Button>
        </div>
      </form>
      <StepUpDialog
        open={stepUp}
        title="Change your email"
        description="Confirm it's you with your two-factor code."
        actionLabel="Continue"
        onClose={() => setStepUp(false)}
        onVerified={async (token) => {
          setStepUp(false)
          await send(token)
        }}
      />
    </Section>
  )
}

// ---------------------------------------------------------------- your data

const STATUS: Record<string, { label: string; tone: 'neutral' | 'success' | 'danger' | 'warning' }> = {
  pending: { label: 'Preparing', tone: 'neutral' },
  running: { label: 'Preparing', tone: 'neutral' },
  ready: { label: 'Ready', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
  expired: { label: 'Expired', tone: 'warning' },
}

export function YourDataPanel() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const exports = useQuery({
    queryKey: exportsKey,
    queryFn: listExports,
    // Check back while one is being prepared.
    refetchInterval: (q) => (q.state.data?.some((e) => e.status === 'pending' || e.status === 'running') ? 3000 : false),
  })
  const start = useMutation({
    mutationFn: requestExport,
    onSuccess: async () => {
      toast.success("We're preparing your export. We'll email you when it's ready.")
      await queryClient.invalidateQueries({ queryKey: exportsKey })
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  const download = async (id: string) => {
    try {
      window.location.assign(await exportLink(id))
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }
  const preparing = exports.data?.some((e) => e.status === 'pending' || e.status === 'running')

  return (
    <Section
      id="your-data"
      title="Your data"
      description="Download a copy of your account, your conversations in every workspace and the files in your personal workspace, as a ZIP file."
    >
      <div className="stack stack--sm">
        <div>
          <Button icon="download" loading={start.isPending} disabled={preparing} onClick={() => start.mutate()}>
            {preparing ? 'Preparing your export…' : 'Request a data export'}
          </Button>
        </div>
        {exports.data && exports.data.length > 0 && (
          <ul className="list export-list" aria-label="Your exports">
            {exports.data.map((e) => {
              const s = STATUS[e.status] ?? { label: e.status, tone: 'neutral' as const }
              return (
                <li key={e.id} className="list-row">
                  <div className="list-row__main">
                    <span className="list-row__title">Export of {formatDateTime(e.created_at)}</span>
                    <span className="list-row__meta">
                      {e.status === 'ready' && e.expires_at
                        ? `${formatBytes(e.size_bytes ?? 0)} · available until ${formatDateTime(e.expires_at)}`
                        : (e.error ?? (e.status === 'expired' ? 'Deleted after a few days. Request a new one.' : 'This takes a minute or two.'))}
                    </span>
                  </div>
                  <Badge tone={s.tone}>{s.label}</Badge>
                  {e.status === 'ready' && (
                    <Button size="sm" icon="download" onClick={() => void download(e.id)}>
                      Download
                    </Button>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </Section>
  )
}

// ---------------------------------------------------------------- delete account

export function DeleteAccountPanel() {
  const { signOut } = useAuth()
  const navigate = useNavigate()
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [stepUp, setStepUp] = useState(false)

  const close = () => {
    setOpen(false)
    setPassword('')
    setConfirm('')
    setError(null)
  }

  const run = async (token?: string) => {
    setBusy(true)
    setError(null)
    try {
      await deleteAccount({ password, confirm }, token)
      // Leave the signed-in area first, so the sign-in guard doesn't redirect to /login.
      navigate('/', { replace: true })
      await signOut()
      toast.success('Your account was deleted.')
    } catch (err) {
      if (needsStepUp(err)) {
        setOpen(false)
        setStepUp(true)
      } else setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section
      id="delete-account"
      danger
      title="Delete account"
      description="Permanently deletes your account, your personal workspace with its documents, and your conversations everywhere. Organizations keep the documents you shared with them."
    >
      <Button variant="danger" icon="trash" onClick={() => setOpen(true)}>
        Delete my account
      </Button>
      <Dialog
        open={open}
        title="Delete your account?"
        description="This can't be undone. Consider downloading your data first."
        onClose={close}
        footer={
          <>
            <Button variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button variant="danger" type="submit" form="delete-account-form" loading={busy} disabled={!password || confirm !== 'DELETE'}>
              Delete account
            </Button>
          </>
        }
      >
        <form
          id="delete-account-form"
          className="form"
          onSubmit={(e) => {
            e.preventDefault()
            void run()
          }}
        >
          {error && <Alert>{error}</Alert>}
          <Field label="Current password">
            <TextInput type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
          <Field label='Type "DELETE" to confirm'>
            <TextInput value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" />
          </Field>
        </form>
      </Dialog>
      <StepUpDialog
        open={stepUp}
        title="Delete your account"
        description="Confirm it's you with your two-factor code."
        actionLabel="Delete account"
        onClose={() => setStepUp(false)}
        onVerified={async (token) => {
          setStepUp(false)
          await run(token)
        }}
      />
    </Section>
  )
}

// ---------------------------------------------------------------- delete organization

export function DeleteOrganizationPanel() {
  const { workspace, workspaces, base } = useWorkspace()
  const user = useUser()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [stepUp, setStepUp] = useState(false)
  const has2fa = Boolean(user.mfa_enabled)

  return (
    <Section
      id="delete-organization"
      danger
      title="Delete organization"
      description="Permanently deletes this organization: its documents, collections, conversations, API keys and connectors. Members lose access, and the subscription is cancelled."
    >
      {has2fa ? (
        <Button variant="danger" icon="trash" onClick={() => setOpen(true)}>
          Delete {workspace.name}
        </Button>
      ) : (
        <Alert tone="info">
          Deleting an organization needs two-factor authentication. <a href={`${base}/profile#security`}>Set it up</a> first.
        </Alert>
      )}
      <Dialog
        open={open}
        title={`Delete ${workspace.name}?`}
        description="This can't be undone. Members are told by email."
        onClose={() => {
          setOpen(false)
          setName('')
        }}
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              disabled={name !== workspace.name}
              onClick={() => {
                setOpen(false)
                setStepUp(true)
              }}
            >
              Continue
            </Button>
          </>
        }
      >
        <Field label={`Type "${workspace.name}" to confirm`}>
          <TextInput value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
        </Field>
      </Dialog>
      <StepUpDialog
        open={stepUp}
        title={`Delete ${workspace.name}`}
        description="Confirm with your two-factor code. The organization is deleted right away."
        actionLabel="Delete organization"
        onClose={() => setStepUp(false)}
        onVerified={async (token) => {
          setStepUp(false)
          try {
            await deleteOrganization(workspace.id, name, token)
            toast.success(`${workspace.name} was deleted.`)
            const personal = workspaces.find((w) => w.kind === 'personal')
            navigate(personal ? `/w/${personal.id}/dashboard` : '/app', { replace: true })
            await queryClient.invalidateQueries({ queryKey: workspacesKey })
          } catch (err) {
            toast.error(errorMessage(err))
          }
        }}
      />
    </Section>
  )
}
