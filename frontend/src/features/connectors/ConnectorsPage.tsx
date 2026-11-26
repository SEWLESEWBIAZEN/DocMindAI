/** Keep documents in sync with Google Drive (ADR 0035). Admins and owners. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { Alert, Badge, Button, EmptyState, ErrorState, Icon, Loading, PageHeader, useToast } from '../../components/ui'
import { errorMessage, isStatus } from '../../lib/api/errors'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { formatRelative } from '../documents/format'
import { StepUpDialog } from '../documents/StepUpDialog'
import {
  authorizeDrive,
  configure,
  connectorsKey,
  disconnect,
  ERRORS,
  getConnectors,
  listFolders,
  syncNow,
  type Connection,
  type Folder,
} from './api'

const STATUS: Record<string, { label: string; tone: 'success' | 'warning' | 'danger' | 'neutral' }> = {
  needs_folders: { label: 'Choose folders', tone: 'warning' },
  active: { label: 'Syncing', tone: 'success' },
  paused: { label: 'Paused', tone: 'warning' },
  error: { label: 'Needs attention', tone: 'danger' },
}

function FolderPicker({ connection, onDone }: { connection: Connection; onDone: () => void }) {
  const { workspace } = useWorkspace()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [path, setPath] = useState<Folder[]>([])
  const [chosen, setChosen] = useState<Folder[]>(connection.folders as Folder[])
  const parent = path.length ? path[path.length - 1].id : null
  const folders = useQuery({
    queryKey: ['ws', workspace.id, 'connectors', connection.id, 'folders', parent],
    queryFn: () => listFolders(workspace.id, connection.id, parent),
  })
  const save = useMutation({
    mutationFn: () => configure(workspace.id, connection.id, chosen),
    onSuccess: async () => {
      toast.success('Folders saved. The first sync has started.')
      await queryClient.invalidateQueries({ queryKey: connectorsKey(workspace.id) })
      onDone()
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  const toggle = (f: Folder, on: boolean) => setChosen(on ? [...chosen, f] : chosen.filter((x) => x.id !== f.id))
  return (
    <div className="stack">
      <nav aria-label="Folder path" className="row">
        <button type="button" className="link-button" onClick={() => setPath([])}>
          Drives
        </button>
        {path.map((f, i) => (
          <span key={f.id}>
            {' / '}
            <button type="button" className="link-button" onClick={() => setPath(path.slice(0, i + 1))}>
              {f.name}
            </button>
          </span>
        ))}
      </nav>
      {folders.isLoading ? (
        <Loading lines={3} />
      ) : folders.error ? (
        <ErrorState error={folders.error} onRetry={() => void folders.refetch()} />
      ) : (
        <ul className="doc-picker" aria-label="Folders">
          {folders.data?.length === 0 && <li className="muted">No folders here.</li>}
          {folders.data?.map((f) => (
            <li key={f.id} className="row" style={{ justifyContent: 'space-between' }}>
              <label className="checkbox">
                <input type="checkbox" checked={chosen.some((c) => c.id === f.id)} onChange={(e) => toggle(f, e.target.checked)} />
                {f.name}
              </label>
              <Button size="sm" variant="ghost" onClick={() => setPath([...path, f])}>
                Open <span className="sr-only">{f.name}</span>
              </Button>
            </li>
          ))}
        </ul>
      )}
      <p className="muted" aria-live="polite">
        {chosen.length ? `Syncing: ${chosen.map((c) => c.name).join(', ')} (with subfolders)` : 'No folders chosen yet.'}
      </p>
      <div>
        <Button variant="primary" loading={save.isPending} disabled={!chosen.length} onClick={() => save.mutate()}>
          Save and sync
        </Button>
      </div>
    </div>
  )
}

function ConnectionPanel({ c, onDisconnect }: { c: Connection; onDisconnect: () => void }) {
  const { workspace, base } = useWorkspace()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [picking, setPicking] = useState(c.status === 'needs_folders')
  const sync = useMutation({
    mutationFn: () => syncNow(workspace.id, c.id),
    onSuccess: async () => {
      toast.success('Sync started.')
      await queryClient.invalidateQueries({ queryKey: connectorsKey(workspace.id) })
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  return (
    <section className="panel" aria-labelledby={`conn-${c.id}`}>
      <header className="panel__header">
        <div>
          <h2 id={`conn-${c.id}`}>
            Google Drive <Badge tone={STATUS[c.status].tone}>{STATUS[c.status].label}</Badge>
          </h2>
          <p className="panel__description">{c.account_email ?? 'Connected account'}</p>
        </div>
        <span className="row">
          {c.status !== 'needs_folders' && (
            <>
              <Button icon="refresh" loading={sync.isPending} onClick={() => sync.mutate()}>
                Sync now
              </Button>
              <Button variant="ghost" onClick={() => setPicking(!picking)}>
                {picking ? 'Close folders' : 'Change folders'}
              </Button>
            </>
          )}
          <Button variant="ghost" icon="trash" onClick={onDisconnect}>
            Disconnect
          </Button>
        </span>
      </header>
      <div className="panel__body stack">
        {c.last_error && <Alert tone={c.last_error === 'unavailable' ? 'info' : 'warning'}>{ERRORS[c.last_error] ?? c.last_error}</Alert>}
        {c.status !== 'needs_folders' && (
          <dl className="billing-facts">
            <div>
              <dt>Folders</dt>
              <dd>{(c.folders as Folder[]).map((f) => f.name).join(', ')}</dd>
            </div>
            <div>
              <dt>Last sync</dt>
              <dd>{c.last_sync_at ? formatRelative(c.last_sync_at) : c.status === 'active' ? 'Starting…' : 'Not yet'}</dd>
            </div>
            <div>
              <dt>Files imported</dt>
              <dd>
                {c.files_synced}
                {c.files_skipped > 0 && <span className="muted"> ({c.files_skipped} skipped: unsupported or unreadable)</span>}
              </dd>
            </div>
            {c.collection_id && (
              <div>
                <dt>Collection</dt>
                <dd>
                  <Link to={`${base}/collections/${c.collection_id}`}>Open the synced documents</Link>
                </dd>
              </div>
            )}
          </dl>
        )}
        {picking && <FolderPicker connection={c} onDone={() => setPicking(false)} />}
      </div>
    </section>
  )
}

export function ConnectorsPage() {
  const { workspace, can } = useWorkspace()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [step, setStep] = useState<{ kind: 'connect' } | { kind: 'disconnect'; id: string } | null>(null)
  useEffect(() => {
    document.title = 'Connectors · DocuMind'
  }, [])
  const query = useQuery({
    queryKey: connectorsKey(workspace.id),
    queryFn: () => getConnectors(workspace.id),
    enabled: can('workspace.update'),
    refetchInterval: (q) => (q.state.data?.connections.some((c) => c.status === 'active' && !c.initial_sync_done) ? 4000 : false),
  })
  const error = params.get('connector_error')
  const connected = params.get('connected')

  if (!can('workspace.update') || isStatus(query.error, 403)) {
    return (
      <>
        <PageHeader title="Connectors" />
        <EmptyState icon="lock" title="Only owners and admins manage connectors" />
      </>
    )
  }
  if (query.isLoading) return <><PageHeader title="Connectors" /><Loading /></>
  if (query.error || !query.data) return <><PageHeader title="Connectors" /><ErrorState error={query.error} onRetry={() => void query.refetch()} /></>
  const data = query.data
  const drive = data.providers[0]

  const onVerified = async (token: string) => {
    const current = step
    setStep(null)
    try {
      if (current?.kind === 'connect') {
        window.location.assign(await authorizeDrive(workspace.id, token))
      } else if (current?.kind === 'disconnect') {
        await disconnect(workspace.id, current.id, token)
        toast.success('Disconnected. Imported documents stay in the workspace.')
        await queryClient.invalidateQueries({ queryKey: connectorsKey(workspace.id) })
      }
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  return (
    <>
      <PageHeader
        title="Connectors"
        description="Keep documents in sync with the places your team already keeps them. Changes come across automatically."
      />
      <div className="stack">
        {error && (
          <Alert>
            {ERRORS[error] ?? 'Connecting failed.'}{' '}
            <button type="button" className="link-button" onClick={() => setParams({}, { replace: true })}>
              Dismiss
            </button>
          </Alert>
        )}
        {connected && <Alert tone="success">Google Drive is connected. Choose the folders to sync.</Alert>}
        {data.connections.map((c) => (
          <ConnectionPanel key={c.id} c={c} onDisconnect={() => setStep({ kind: 'disconnect', id: c.id })} />
        ))}
        <section className="panel" aria-labelledby="add-connector">
          <header className="panel__header">
            <div>
              <h2 id="add-connector">
                <Icon name="layers" size={17} /> Google Drive
              </h2>
              <p className="panel__description">
                Read-only access. PDFs, Word files, text, Markdown and Google Docs in the folders you choose are imported and kept up to
                date; files removed from those folders are archived here, never deleted.
              </p>
            </div>
            {drive.available && (
              <Button variant="primary" onClick={() => setStep({ kind: 'connect' })}>
                {data.connections.length ? 'Connect another account' : 'Connect Google Drive'}
              </Button>
            )}
          </header>
          {!drive.available && (
            <div className="panel__body">
              <Alert tone="info">
                Google Drive isn't set up on this server yet. An administrator adds a Google OAuth client (see the configuration guide); its
                redirect URI is <code>{drive.redirect_uri}</code>.
              </Alert>
            </div>
          )}
        </section>
      </div>
      <StepUpDialog
        open={step !== null}
        title={step?.kind === 'disconnect' ? 'Disconnect Google Drive?' : 'Connect Google Drive'}
        description={
          step?.kind === 'disconnect'
            ? 'Syncing stops and DocuMind gives up its access. Imported documents stay. Confirm with your two-factor code.'
            : 'You will choose a Google account next. Confirm with your two-factor code first.'
        }
        actionLabel={step?.kind === 'disconnect' ? 'Disconnect' : 'Continue to Google'}
        onClose={() => setStep(null)}
        onVerified={onVerified}
      />
    </>
  )
}
