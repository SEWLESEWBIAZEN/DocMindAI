/** API keys, the MCP server and the Slack app (ADR 0037). */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'

import {
  Alert,
  Badge,
  Button,
  Dialog,
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
import { errorMessage } from '../../lib/api/errors'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { formatRelative } from '../documents/format'
import { StepUpDialog } from '../documents/StepUpDialog'
import {
  authorizeSlack,
  createKey,
  getIntegrations,
  integrationsKey,
  keysKey,
  linkCode,
  listKeys,
  mcpConfig,
  removeSlack,
  revokeKey,
  SLACK_ERRORS,
  unlinkSlack,
  type ApiKeyRequest,
  type Integrations,
  type LinkCode,
  type NewApiKey,
} from './api'

function CopyBlock({ label, value }: { label: string; value: string }) {
  const toast = useToast()
  return (
    <div className="copy-block">
      <pre aria-label={label} tabIndex={0}>
        <code>{value}</code>
      </pre>
      <Button
        size="sm"
        icon="copy"
        onClick={() => void navigator.clipboard?.writeText(value).then(() => toast.success('Copied.'), () => toast.error("Couldn't copy."))}
      >
        Copy <span className="sr-only">{label}</span>
      </Button>
    </div>
  )
}

const EXPIRY = [
  { value: '30', label: '30 days' },
  { value: '90', label: '90 days' },
  { value: '365', label: '1 year' },
  { value: 'never', label: 'Never' },
]

function NewKeyDialog({
  open,
  scopes,
  onClose,
  onSubmit,
}: {
  open: boolean
  scopes: Integrations['scopes']
  onClose: () => void
  onSubmit: (body: ApiKeyRequest) => void
}) {
  const [name, setName] = useState('')
  const [chosen, setChosen] = useState<string[]>(['documents:read', 'ask'])
  const [expiry, setExpiry] = useState('90')
  const legend = useId()
  const submit = (e: FormEvent) => {
    e.preventDefault()
    onSubmit({
      name: name.trim(),
      scopes: chosen as ApiKeyRequest['scopes'],
      expires_in_days: expiry === 'never' ? null : Number(expiry),
    })
  }
  return (
    <Dialog
      open={open}
      title="New API key"
      description="The key acts as you, limited to what you choose here. It can't manage members, billing or settings."
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" type="submit" form="new-key" disabled={!name.trim() || !chosen.length}>
            Create key
          </Button>
        </>
      }
    >
      <form id="new-key" className="stack" onSubmit={submit}>
        <Field label="Name" hint="Where it's used, e.g. “Zapier” or “Claude Desktop”.">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} maxLength={80} required autoFocus />
        </Field>
        <fieldset className="stack" aria-labelledby={legend}>
          <legend id={legend}>What it can do</legend>
          {scopes.map((s) => (
            <label key={s.key} className="checkbox">
              <input
                type="checkbox"
                checked={chosen.includes(s.key)}
                onChange={(e) => setChosen(e.target.checked ? [...chosen, s.key] : chosen.filter((c) => c !== s.key))}
              />
              <span>
                <code>{s.key}</code>: {s.description}
              </span>
            </label>
          ))}
        </fieldset>
        <Field label="Expires">
          <Select value={expiry} onChange={(e) => setExpiry(e.target.value)}>
            {EXPIRY.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </Select>
        </Field>
      </form>
    </Dialog>
  )
}

function ApiKeysPanel({ data }: { data: Integrations }) {
  const { workspace } = useWorkspace()
  const toast = useToast()
  const confirm = useConfirm()
  const queryClient = useQueryClient()
  const [form, setForm] = useState(false)
  const [pending, setPending] = useState<ApiKeyRequest | null>(null)
  const [created, setCreated] = useState<NewApiKey | null>(null)
  const keys = useQuery({ queryKey: keysKey(workspace.id), queryFn: () => listKeys(workspace.id) })
  const revoke = useMutation({
    mutationFn: (id: string) => revokeKey(workspace.id, id),
    onSuccess: async () => {
      toast.success('Key revoked. Anything using it stops working now.')
      await queryClient.invalidateQueries({ queryKey: keysKey(workspace.id) })
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  const onVerified = async (token: string) => {
    const body = pending
    setPending(null)
    if (!body) return
    try {
      setCreated(await createKey(workspace.id, body, token))
      await queryClient.invalidateQueries({ queryKey: keysKey(workspace.id) })
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }
  return (
    <section className="panel" aria-labelledby="api-keys">
      <header className="panel__header">
        <div>
          <h2 id="api-keys">
            <Icon name="key" size={17} /> API keys
          </h2>
          <p className="panel__description">
            For scripts, automations and AI assistants. Send a key as <code>Authorization: Bearer dmk_…</code> to{' '}
            <code>{data.api_base_url}</code>.
          </p>
        </div>
        <Button variant="primary" icon="plus" onClick={() => setForm(true)}>
          New key
        </Button>
      </header>
      <div className="panel__body">
        {keys.isLoading ? (
          <Loading lines={2} />
        ) : keys.error ? (
          <ErrorState error={keys.error} onRetry={() => void keys.refetch()} />
        ) : !keys.data?.length ? (
          <p className="muted">No keys yet.</p>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <caption className="sr-only">API keys</caption>
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Key</th>
                  <th scope="col">Can</th>
                  <th scope="col">Last used</th>
                  <th scope="col">Expires</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {keys.data.map((k) => (
                  <tr key={k.id}>
                    <td>{k.name}</td>
                    <td>
                      <code>{k.prefix}…</code>
                    </td>
                    <td>{k.scopes.join(', ')}</td>
                    <td>{k.last_used_at ? formatRelative(k.last_used_at) : 'Never'}</td>
                    <td>{k.expires_at ? new Date(k.expires_at).toLocaleDateString() : 'Never'}</td>
                    <td>
                      <Button
                        size="sm"
                        variant="ghost"
                        icon="trash"
                        onClick={async () => {
                          const ok = await confirm({
                            title: `Revoke “${k.name}”?`,
                            description: 'Anything using this key stops working immediately.',
                            confirmLabel: 'Revoke',
                            danger: true,
                          })
                          if (ok) revoke.mutate(k.id)
                        }}
                      >
                        Revoke <span className="sr-only">{k.name}</span>
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <NewKeyDialog
        open={form}
        scopes={data.scopes}
        onClose={() => setForm(false)}
        onSubmit={(body) => {
          setForm(false)
          setPending(body)
        }}
      />
      <StepUpDialog
        open={pending !== null}
        title="Create API key"
        description="Confirm with your two-factor code."
        actionLabel="Create key"
        onClose={() => setPending(null)}
        onVerified={onVerified}
      />
      <Dialog
        open={created !== null}
        title="Copy your key now"
        description="It won't be shown again. Store it somewhere safe, like a password manager or your tool's secret settings."
        onClose={() => setCreated(null)}
        footer={
          <Button variant="primary" onClick={() => setCreated(null)}>
            Done
          </Button>
        }
      >
        {created && <CopyBlock label="New API key" value={created.token} />}
      </Dialog>
    </section>
  )
}

function McpPanel({ data }: { data: Integrations }) {
  return (
    <section className="panel" aria-labelledby="mcp">
      <header className="panel__header">
        <div>
          <h2 id="mcp">
            <Icon name="sparkles" size={17} /> MCP server
          </h2>
          <p className="panel__description">
            Let AI assistants that support the Model Context Protocol search this workspace, read documents and ask cited questions. Use an
            API key with <code>documents:read</code> (and <code>ask</code> for answers).
          </p>
        </div>
      </header>
      <div className="panel__body stack">
        <CopyBlock label="MCP server URL" value={data.mcp_url} />
        <p className="muted">For apps configured with a JSON file:</p>
        <CopyBlock label="MCP configuration" value={mcpConfig(data.mcp_url)} />
        <p className="muted">For Claude Code:</p>
        <CopyBlock
          label="Claude Code command"
          value={`claude mcp add --transport http documind ${data.mcp_url} --header "Authorization: Bearer YOUR_API_KEY"`}
        />
      </div>
    </section>
  )
}

function SlackPanel({ data, onStepUp }: { data: Integrations; onStepUp: (kind: 'add' | 'remove') => void }) {
  const { workspace } = useWorkspace()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [code, setCode] = useState<LinkCode | null>(null)
  const slack = data.slack
  const getCode = useMutation({
    mutationFn: () => linkCode(workspace.id),
    onSuccess: setCode,
    onError: (err) => toast.error(errorMessage(err)),
  })
  const unlink = useMutation({
    mutationFn: () => unlinkSlack(workspace.id),
    onSuccess: async () => {
      toast.success('Your Slack account is unlinked.')
      await queryClient.invalidateQueries({ queryKey: integrationsKey(workspace.id) })
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  return (
    <section className="panel" aria-labelledby="slack">
      <header className="panel__header">
        <div>
          <h2 id="slack">
            <Icon name="message" size={17} /> Slack{' '}
            {slack.connected && <Badge tone="success">Connected{slack.team_name ? ` to ${slack.team_name}` : ''}</Badge>}
          </h2>
          <p className="panel__description">
            Ask with <code>/documind your question</code> for a private answer, or mention <code>@DocuMind</code> in a channel for an
            answer in the thread. Channel answers use only documents everyone in the workspace can see.
          </p>
        </div>
        {data.can_manage && slack.available && (
          <span className="row">
            {slack.connected ? (
              <Button variant="ghost" icon="trash" onClick={() => onStepUp('remove')}>
                Remove from Slack
              </Button>
            ) : (
              <Button variant="primary" onClick={() => onStepUp('add')}>
                Add to Slack
              </Button>
            )}
          </span>
        )}
      </header>
      <div className="panel__body stack">
        {!slack.available ? (
          <Alert tone="info">Slack isn't set up on this DocuMind server yet. An administrator adds a Slack app (see the configuration guide).</Alert>
        ) : !slack.connected ? (
          <p className="muted">{data.can_manage ? 'Add the app to your Slack workspace to start.' : 'An owner or admin adds the app to Slack.'}</p>
        ) : slack.linked ? (
          <div className="row">
            <span>
              <Icon name="check" size={15} /> Your Slack account is linked.
            </span>
            <Button size="sm" variant="ghost" loading={unlink.isPending} onClick={() => unlink.mutate()}>
              Unlink
            </Button>
          </div>
        ) : code ? (
          <div className="stack" aria-live="polite">
            <p>Run this in Slack within 10 minutes:</p>
            <CopyBlock label="Slack link command" value={code.command} />
          </div>
        ) : (
          <div>
            <p className="muted">Link your Slack account so answers use what you can see here.</p>
            <Button loading={getCode.isPending} onClick={() => getCode.mutate()}>
              Link my Slack account
            </Button>
          </div>
        )}
      </div>
    </section>
  )
}

export function IntegrationsPage() {
  const { workspace, can, base } = useWorkspace()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [step, setStep] = useState<'add' | 'remove' | null>(null)
  useEffect(() => {
    document.title = 'Integrations · DocuMind'
  }, [])
  const query = useQuery({ queryKey: integrationsKey(workspace.id), queryFn: () => getIntegrations(workspace.id) })
  const header = (
    <PageHeader title="Integrations" description="Use this workspace from scripts, AI assistants and Slack, with the same permissions as the app." />
  )
  if (query.isLoading) return <>{header}<Loading /></>
  if (query.error || !query.data) return <>{header}<ErrorState error={query.error} onRetry={() => void query.refetch()} /></>
  const data = query.data
  const error = params.get('slack_error')

  const onVerified = async (token: string) => {
    const kind = step
    setStep(null)
    try {
      if (kind === 'add') window.location.assign(await authorizeSlack(workspace.id, token))
      else if (kind === 'remove') {
        await removeSlack(workspace.id, token)
        toast.success('DocuMind was removed from Slack.')
        await queryClient.invalidateQueries({ queryKey: integrationsKey(workspace.id) })
      }
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  return (
    <>
      {header}
      <div className="stack">
        {error && (
          <Alert>
            {SLACK_ERRORS[error] ?? 'Adding the Slack app failed.'}{' '}
            <button type="button" className="link-button" onClick={() => setParams({}, { replace: true })}>
              Dismiss
            </button>
          </Alert>
        )}
        {params.get('slack') === 'connected' && <Alert tone="success">DocuMind is in your Slack workspace. Link your account below.</Alert>}
        {!data.included ? (
          <EmptyState
            icon="lock"
            title="Integrations are included from the Pro plan"
            description="API keys, the MCP server and Slack come with Pro, Business and Enterprise, and with the free Pro trial."
            action={
              can('billing.manage') ? (
                <Link className="btn btn--primary btn--md" to={`${base}/billing`}>
                  See plans
                </Link>
              ) : undefined
            }
          />
        ) : (
          <>
            <SlackPanel data={data} onStepUp={setStep} />
            {data.can_manage ? (
              <>
                <ApiKeysPanel data={data} />
                <McpPanel data={data} />
              </>
            ) : (
              <Alert tone="info">Owners and admins create API keys and set up the MCP server.</Alert>
            )}
          </>
        )}
      </div>
      <StepUpDialog
        open={step !== null}
        title={step === 'remove' ? 'Remove DocuMind from Slack?' : 'Add DocuMind to Slack'}
        description={
          step === 'remove'
            ? 'The app stops answering and linked accounts are forgotten. Confirm with your two-factor code.'
            : 'You will choose a Slack workspace next. Confirm with your two-factor code first.'
        }
        actionLabel={step === 'remove' ? 'Remove' : 'Continue to Slack'}
        onClose={() => setStep(null)}
        onVerified={onVerified}
      />
    </>
  )
}
