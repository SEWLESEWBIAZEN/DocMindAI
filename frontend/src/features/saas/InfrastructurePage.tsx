import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState, type FormEvent } from 'react'

import {
  Alert,
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Field,
  Loading,
  PageHeader,
  Select,
  TextInput,
  useToast,
  type BadgeTone,
} from '../../components/ui'
import { ApiError, errorMessage, isStatus } from '../../lib/api/errors'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { formatDateTime } from '../documents/format'
import { StepUpDialog } from '../documents/StepUpDialog'
import {
  activateVersion,
  cancelMigration,
  createVersion,
  getInfrastructure,
  infrastructureKey,
  registerWorker,
  revokeWorker,
  validateVersion,
  type CreateVersion,
  type Infrastructure,
  type InfrastructureVersion,
} from './api'

type DbMode = 'shared' | 'dedicated' | 'external'
type StorageProvider = 'documind' | 'aws_s3' | 's3_compatible' | 'minio'
type Pending =
  | { kind: 'create'; body: CreateVersion }
  | { kind: 'activate'; version: InfrastructureVersion }
  | { kind: 'worker'; name: string }

const STATUS_TONE: Record<string, BadgeTone> = {
  draft: 'neutral',
  validated: 'accent',
  failed: 'danger',
  active: 'success',
  retired: 'neutral',
}

const DB_LABEL: Record<string, string> = {
  shared: 'DocuMind shared database',
  dedicated: 'Dedicated database (DocuMind-managed)',
  external: 'Your own PostgreSQL',
}

function describe(config: Record<string, unknown>): string {
  const db = (config.database ?? {}) as Record<string, unknown>
  const st = (config.storage ?? {}) as Record<string, unknown>
  const pr = (config.processing ?? {}) as Record<string, unknown>
  const where = db.mode === 'external' ? ` (${String(db.host)}/${String(db.database)})` : ''
  const bucket = st.provider === 'documind' ? 'DocuMind storage' : `${String(st.provider)}: ${String(st.bucket)}${st.prefix ? `/${String(st.prefix)}` : ''}`
  return `${DB_LABEL[String(db.mode)] ?? String(db.mode)}${where} · ${bucket} · ${pr.mode === 'documind' ? 'DocuMind processing' : `${String(pr.mode)} processing`}`
}

function ValidationResults({ validation }: { validation: Record<string, unknown> }) {
  const parts = ['database', 'storage', 'processing'] as const
  const rows = parts
    .map((p) => [p, validation[p] as { ok?: boolean; message?: string | null; code?: string | null } | undefined] as const)
    .filter(([, v]) => v)
  if (rows.length === 0) return null
  return (
    <ul className="check-list">
      {rows.map(([part, v]) => (
        <li key={part} className={v?.ok ? 'check-list__ok' : 'check-list__fail'}>
          <span className="sr-only">{v?.ok ? 'Passed: ' : 'Failed: '}</span>
          {part[0].toUpperCase() + part.slice(1)}: {v?.ok ? 'OK' : (v?.message ?? v?.code ?? 'failed')}
        </li>
      ))}
    </ul>
  )
}

/** Organization infrastructure settings (MI-5, ADR 0021). Owners only. */
export function InfrastructurePage() {
  useEffect(() => {
    document.title = 'Infrastructure · DocuMind'
  }, [])
  const { workspace, can } = useWorkspace()
  const queryClient = useQueryClient()
  const toast = useToast()
  const query = useQuery({
    queryKey: infrastructureKey(workspace.id),
    queryFn: () => getInfrastructure(workspace.id),
    enabled: can('infrastructure.manage') && workspace.kind === 'organization',
  })
  const [pending, setPending] = useState<Pending | null>(null)
  const [issuedToken, setIssuedToken] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  // Form state for a new version.
  const [dbMode, setDbMode] = useState<DbMode>('shared')
  const [db, setDb] = useState({ host: '', port: '5432', database: '', username: '', password: '', sslmode: 'verify-full', manage: false })
  const [provider, setProvider] = useState<StorageProvider>('documind')
  const [st, setSt] = useState({ bucket: '', region: '', endpoint: '', prefix: 'documind', accessKey: '', secret: '' })
  const [processing, setProcessing] = useState<'documind' | 'customer' | 'hybrid'>('documind')
  const [note, setNote] = useState('')
  const [workerName, setWorkerName] = useState('')
  const [formError, setFormError] = useState<string | null>(null)

  const refresh = () => queryClient.invalidateQueries({ queryKey: infrastructureKey(workspace.id) })

  if (workspace.kind !== 'organization') {
    return (
      <>
        <PageHeader title="Infrastructure" />
        <EmptyState icon="building" title="Infrastructure settings are for organizations" description="Personal workspaces use DocuMind's managed infrastructure." />
      </>
    )
  }
  if (!can('infrastructure.manage') || isStatus(query.error, 403)) {
    return (
      <>
        <PageHeader title="Infrastructure" />
        <EmptyState icon="lock" title="Only owners manage infrastructure" />
      </>
    )
  }
  if (query.isLoading) return <><PageHeader title="Infrastructure" /><Loading label="Loading infrastructure" /></>
  if (query.error || !query.data) return <><PageHeader title="Infrastructure" /><ErrorState error={query.error} onRetry={() => void refresh()} /></>
  const data: Infrastructure = query.data
  const ents = data.entitlements

  const buildBody = (): CreateVersion => ({
    note: note.trim() || null,
    database:
      dbMode === 'external'
        ? {
            mode: 'external',
            host: db.host.trim(),
            port: Number(db.port) || 5432,
            database: db.database.trim(),
            username: db.username.trim(),
            password: db.password || null,
            sslmode: db.sslmode as 'verify-full',
            manage_schema: db.manage,
          }
        : { mode: dbMode, manage_schema: false },
    storage:
      provider === 'documind'
        ? { provider: 'documind', prefix: '' }
        : {
            provider,
            bucket: st.bucket.trim(),
            region: st.region.trim() || null,
            endpoint_url: provider === 'aws_s3' ? null : st.endpoint.trim(),
            prefix: st.prefix.trim(),
            access_key_id: st.accessKey.trim(),
            secret_access_key: st.secret || null,
          },
    processing: { mode: processing },
  })

  const submit = (event: FormEvent) => {
    event.preventDefault()
    setFormError(null)
    setPending({ kind: 'create', body: buildBody() })
  }

  const onVerified = async (token: string) => {
    if (!pending) return
    try {
      if (pending.kind === 'create') {
        const v = await createVersion(workspace.id, pending.body, token)
        setDb((d) => ({ ...d, password: '' }))
        setSt((s) => ({ ...s, secret: '' }))
        toast.success(`Version ${v.version} saved as a draft. Validate it next.`)
      } else if (pending.kind === 'activate') {
        await activateVersion(workspace.id, pending.version.id, token)
        toast.success(`Version ${pending.version.version} is now active.`)
      } else {
        const issued = await registerWorker(workspace.id, pending.name, token)
        setIssuedToken(issued.token)
        setWorkerName('')
      }
    } catch (err) {
      if (err instanceof ApiError && err.code === 'migration_required') {
        toast.error(err.message)
      } else if (pending.kind === 'create') {
        setFormError(errorMessage(err))
      } else {
        toast.error(errorMessage(err))
      }
    } finally {
      setPending(null)
      await refresh()
    }
  }

  const validate = async (v: InfrastructureVersion) => {
    setBusy(v.id)
    try {
      const result = await validateVersion(workspace.id, v.id)
      if (result.status === 'validated') toast.success(`Version ${v.version} passed every check.`)
      else toast.error(`Version ${v.version} failed validation. See the results below.`)
    } catch (err) {
      toast.error(errorMessage(err))
    } finally {
      setBusy(null)
      await refresh()
    }
  }

  return (
    <>
      <PageHeader
        title="Infrastructure"
        description="Choose where this organization's documents, search index and conversations are stored and processed. Identity and access always stay with DocuMind."
      />
      <div className="stack" style={{ gap: 24 }}>
        <section className="panel" aria-labelledby="current-heading">
          <header className="panel__header">
            <div>
              <h2 id="current-heading">Current infrastructure</h2>
              <p className="panel__description">{describe(data.active_config)}</p>
            </div>
            <Badge tone={data.active_version_id ? 'success' : 'neutral'}>
              {data.active_version_id ? 'Custom' : 'DocuMind defaults'}
            </Badge>
          </header>
          {data.has_data && (
            <div className="panel__body">
              <Alert tone="info">
                This organization already has data. Changing the database or storage location needs a planned migration;
                DocuMind never moves your data without your explicit approval.
              </Alert>
            </div>
          )}
        </section>

        <section className="panel" aria-labelledby="new-heading">
          <header className="panel__header">
            <div>
              <h2 id="new-heading">New configuration</h2>
              <p className="panel__description">
                Saved as an immutable draft version. Passwords and secret keys are stored encrypted and never shown again.
                Saving needs your two-factor code.
              </p>
            </div>
          </header>
          <form className="panel__body stack" onSubmit={submit} noValidate>
            <fieldset className="fieldset">
              <legend>Database</legend>
              <div className="choice-row" role="radiogroup" aria-label="Database">
                {(['shared', 'dedicated', 'external'] as const).map((mode) => {
                  const allowed = mode === 'shared' || (mode === 'dedicated' ? ents.dedicated_database && data.dedicated_available : ents.external_database)
                  return (
                    <label key={mode} className={`choice${dbMode === mode ? ' choice--selected' : ''}${allowed ? '' : ' choice--disabled'}`}>
                      <input type="radio" name="db-mode" value={mode} checked={dbMode === mode} disabled={!allowed} onChange={() => setDbMode(mode)} />
                      <span>{DB_LABEL[mode]}</span>
                      {!allowed && <small>{mode === 'dedicated' && !data.dedicated_available ? 'Not offered on this server' : 'Not in your plan'}</small>}
                    </label>
                  )
                })}
              </div>
              {dbMode === 'external' && (
                <div className="form-grid">
                  <Field label="Host"><TextInput value={db.host} onChange={(e) => setDb({ ...db, host: e.target.value })} autoComplete="off" /></Field>
                  <Field label="Port"><TextInput inputMode="numeric" value={db.port} onChange={(e) => setDb({ ...db, port: e.target.value })} /></Field>
                  <Field label="Database name"><TextInput value={db.database} onChange={(e) => setDb({ ...db, database: e.target.value })} /></Field>
                  <Field label="User" hint="A dedicated role that isn't a superuser."><TextInput value={db.username} onChange={(e) => setDb({ ...db, username: e.target.value })} autoComplete="off" /></Field>
                  <Field label="Password" hint="Leave empty to keep the saved password for the same database."><TextInput type="password" value={db.password} onChange={(e) => setDb({ ...db, password: e.target.value })} autoComplete="new-password" /></Field>
                  <Field label="TLS">
                    <Select value={db.sslmode} onChange={(e) => setDb({ ...db, sslmode: e.target.value })}>
                      <option value="verify-full">Verify certificate and host (recommended)</option>
                      <option value="verify-ca">Verify certificate</option>
                      <option value="require">Encrypt only</option>
                      <option value="disable">No TLS (development only)</option>
                    </Select>
                  </Field>
                  <label className="checkbox">
                    <input type="checkbox" checked={db.manage} onChange={(e) => setDb({ ...db, manage: e.target.checked })} />
                    DocuMind may create and migrate its tables (in the documind schema)
                  </label>
                </div>
              )}
            </fieldset>

            <fieldset className="fieldset">
              <legend>Document storage</legend>
              <Field label="Provider">
                <Select value={provider} onChange={(e) => setProvider(e.target.value as StorageProvider)}>
                  {data.storage_providers.map((p) => (
                    <option key={p.key} value={p.key} disabled={p.status !== 'available' || (p.key !== 'documind' && !ents.custom_storage)}>
                      {p.name}
                      {p.status !== 'available' ? ' (planned)' : p.key !== 'documind' && !ents.custom_storage ? ' (not in your plan)' : ''}
                    </option>
                  ))}
                </Select>
              </Field>
              {provider !== 'documind' && (
                <div className="form-grid">
                  <Field label="Bucket"><TextInput value={st.bucket} onChange={(e) => setSt({ ...st, bucket: e.target.value })} /></Field>
                  {provider === 'aws_s3' ? (
                    <Field label="Region"><TextInput value={st.region} placeholder="eu-central-1" onChange={(e) => setSt({ ...st, region: e.target.value })} /></Field>
                  ) : (
                    <Field label="Endpoint URL" hint="https:// only."><TextInput value={st.endpoint} placeholder="https://storage.example.com" onChange={(e) => setSt({ ...st, endpoint: e.target.value })} /></Field>
                  )}
                  <Field label="Key prefix" hint="DocuMind only writes under this prefix."><TextInput value={st.prefix} onChange={(e) => setSt({ ...st, prefix: e.target.value })} /></Field>
                  <Field label="Access key ID"><TextInput value={st.accessKey} onChange={(e) => setSt({ ...st, accessKey: e.target.value })} autoComplete="off" /></Field>
                  <Field label="Secret access key" hint="Leave empty to keep the saved key for the same bucket."><TextInput type="password" value={st.secret} onChange={(e) => setSt({ ...st, secret: e.target.value })} autoComplete="new-password" /></Field>
                </div>
              )}
            </fieldset>

            <fieldset className="fieldset">
              <legend>Processing</legend>
              <Field
                label="Where documents are processed"
                hint="Your own workers need your own database (above) and a registered worker that has checked in (below)."
              >
                <Select value={processing} onChange={(e) => setProcessing(e.target.value as typeof processing)}>
                  <option value="documind">DocuMind-hosted workers</option>
                  <option value="customer" disabled={!ents.customer_workers}>Your own workers only</option>
                  <option value="hybrid" disabled={!ents.customer_workers}>Hybrid (your workers and DocuMind's)</option>
                </Select>
              </Field>
            </fieldset>
            <Field label="Note (optional)"><TextInput value={note} maxLength={255} onChange={(e) => setNote(e.target.value)} /></Field>
            {formError && <Alert>{formError}</Alert>}
            <div className="row">
              <Button type="submit" variant="primary" icon="plus">Save draft</Button>
            </div>
          </form>
        </section>

        <section className="panel" aria-labelledby="versions-heading">
          <header className="panel__header">
            <div>
              <h2 id="versions-heading">Versions</h2>
              <p className="panel__description">Each version is checked before it can be activated. Activating needs your two-factor code.</p>
            </div>
          </header>
          <div className="panel__body">
            {data.versions.length === 0 ? (
              <p className="muted">No versions yet. This organization uses DocuMind's defaults.</p>
            ) : (
              <ol className="version-list">
                {data.versions.map((v) => (
                  <li key={v.id} className="version">
                    <div className="version__head">
                      <h3>Version {v.version}</h3>
                      <Badge tone={STATUS_TONE[v.status] ?? 'neutral'}>{v.status}</Badge>
                    </div>
                    <p>{describe(v.config)}</p>
                    {v.note && <p className="muted">{v.note}</p>}
                    <p className="muted">Created {formatDateTime(v.created_at)}{v.activated_at ? ` · activated ${formatDateTime(v.activated_at)}` : ''}</p>
                    <ValidationResults validation={v.validation} />
                    <div className="row">
                      {(v.status === 'draft' || v.status === 'failed' || v.status === 'validated') && (
                        <Button size="sm" loading={busy === v.id} onClick={() => void validate(v)}>
                          {v.status === 'draft' ? 'Validate' : 'Validate again'}
                        </Button>
                      )}
                      {v.status === 'validated' && (
                        <Button size="sm" variant="primary" onClick={() => setPending({ kind: 'activate', version: v })}>
                          Activate
                        </Button>
                      )}
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </div>
        </section>

        {data.migrations.length > 0 && (
          <section className="panel" aria-labelledby="migrations-heading">
            <header className="panel__header">
              <div>
                <h2 id="migrations-heading">Planned migrations</h2>
                <p className="panel__description">Recorded when a change would move existing data. Nothing runs without your approval.</p>
              </div>
            </header>
            <div className="panel__body">
              <ul className="plain-list">
                {data.migrations.map((m) => (
                  <li key={String(m.id)} className="row row--between">
                    <span>
                      Move {((m.plan as { moves?: string[] }).moves ?? []).join(' and ')} · {String(m.status)} · {formatDateTime(String(m.created_at))}
                    </span>
                    {(m.status === 'planned' || m.status === 'approved') && (
                      <Button size="sm" onClick={async () => { await cancelMigration(workspace.id, String(m.id)); await refresh() }}>Cancel</Button>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          </section>
        )}

        <section className="panel" aria-labelledby="workers-heading">
          <header className="panel__header">
            <div>
              <h2 id="workers-heading">Customer-hosted workers</h2>
              <p className="panel__description">
                Workers process documents inside your network, reading your own database and storage (documind-worker image). Tokens are shown once and can be revoked.
              </p>
            </div>
          </header>
          <div className="panel__body stack">
            {!ents.customer_workers && <Alert tone="info">Customer-hosted processing is part of the Enterprise plan.</Alert>}
            {issuedToken && (
              <Alert tone="success">
                Worker token (copy it now; it won't be shown again): <code className="token">{issuedToken}</code>
              </Alert>
            )}
            <form
              className="row"
              onSubmit={(e) => {
                e.preventDefault()
                if (workerName.trim()) setPending({ kind: 'worker', name: workerName.trim() })
              }}
            >
              <Field label="Worker name" hideLabel>
                <TextInput placeholder="Worker name" value={workerName} onChange={(e) => setWorkerName(e.target.value)} disabled={!ents.customer_workers} />
              </Field>
              <Button type="submit" disabled={!ents.customer_workers || !workerName.trim()}>Register worker</Button>
            </form>
            {data.workers.length > 0 && (
              <ul className="plain-list">
                {data.workers.map((w) => (
                  <li key={String(w.id)} className="row row--between">
                    <span>
                      {String(w.name)} · <code>{String(w.token_prefix)}…</code> · {String(w.status)}
                    </span>
                    {w.status === 'active' && (
                      <Button size="sm" variant="danger" onClick={async () => { await revokeWorker(workspace.id, String(w.id)); await refresh() }}>Revoke</Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>

        {data.events.length > 0 && (
          <section className="panel" aria-labelledby="events-heading">
            <header className="panel__header">
              <div>
                <h2 id="events-heading">History</h2>
              </div>
            </header>
            <div className="panel__body">
              <ul className="plain-list">
                {data.events.map((e, i) => (
                  <li key={i}>
                    {formatDateTime(String(e.created_at))} · {String(e.kind).replace(/_/g, ' ')}
                    {e.ok === false ? ' (failed)' : ''}
                  </li>
                ))}
              </ul>
            </div>
          </section>
        )}
      </div>
      <StepUpDialog
        open={pending !== null}
        title={pending?.kind === 'activate' ? `Activate version ${pending.version.version}?` : pending?.kind === 'worker' ? 'Register a worker?' : 'Save this configuration?'}
        description={
          pending?.kind === 'activate'
            ? 'New documents and conversations go to this infrastructure right away.'
            : pending?.kind === 'worker'
              ? 'A token is issued for this worker and shown once.'
              : 'Secrets are encrypted and stored. A dedicated database is created now if you chose one.'
        }
        actionLabel={pending?.kind === 'activate' ? 'Activate' : pending?.kind === 'worker' ? 'Register' : 'Save draft'}
        onClose={() => setPending(null)}
        onVerified={onVerified}
      />
    </>
  )
}
