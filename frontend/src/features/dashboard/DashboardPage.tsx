import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'
import { Link, useNavigate } from 'react-router'

import { Alert, Button, EmptyState, ErrorState, Icon, Loading, PageHeader } from '../../components/ui'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { ROLE_LABELS } from '../../lib/workspace/permissions'
import { useUser } from '../auth/AuthProvider'
import { conversationKeys, listConversations } from '../chat/api'
import { documentKeys, listDocuments } from '../documents/api'
import { formatBytes, formatRelative } from '../documents/format'
import { useCollections } from '../documents/hooks'
import { StatusBadge } from '../documents/pages'

function greeting(now = new Date()): string {
  const hour = now.getHours()
  return hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening'
}

export function DashboardPage() {
  const user = useUser()
  const { workspace, base, can } = useWorkspace()
  const navigate = useNavigate()
  useEffect(() => {
    document.title = `Dashboard · DocuMind`
  }, [])
  const ws = workspace.id
  const recent = useQuery({
    queryKey: documentKeys.list(ws, { limit: 6 }),
    queryFn: () => listDocuments(ws, { limit: 6 }),
    enabled: can('documents.read'),
    refetchInterval: (q) => (q.state.data?.items.some((d) => d.status === 'pending' || d.status === 'processing') ? 3000 : false),
  })
  const ready = useQuery({
    queryKey: documentKeys.list(ws, { status: 'ready', limit: 1 }),
    queryFn: () => listDocuments(ws, { status: 'ready', limit: 1 }),
    enabled: can('documents.read'),
  })
  const failed = useQuery({
    queryKey: documentKeys.list(ws, { status: 'failed', limit: 5 }),
    queryFn: () => listDocuments(ws, { status: 'failed', limit: 5 }),
    enabled: can('documents.read'),
  })
  const conversations = useQuery({
    queryKey: conversationKeys.list(ws, '', 0, 5),
    queryFn: () => listConversations(ws, { limit: 5 }),
    enabled: can('conversations.use'),
  })
  const collections = useCollections()

  const stats = [
    { label: 'Documents', value: recent.data?.total, hint: 'in this workspace', to: `${base}/documents` },
    { label: 'Ready to ask', value: ready.data?.total, hint: 'processed and searchable', to: `${base}/documents?status=ready` },
    { label: 'Collections', value: collections.data?.length, hint: 'groups of documents', to: `${base}/collections` },
    { label: 'Conversations', value: conversations.data?.total, hint: 'yours', to: `${base}/conversations` },
  ]

  return (
    <>
      <PageHeader
        title={`${greeting()}, ${user.display_name.split(' ')[0]}`}
        description={
          <>
            {workspace.name} · {workspace.kind === 'personal' ? 'Personal workspace' : 'Organization'} · {ROLE_LABELS[workspace.role]}
          </>
        }
        actions={
          <>
            {can('documents.write') && (
              <Link className="btn btn--secondary btn--md" to={`${base}/upload`}>
                <Icon name="upload" size={17} /> Upload
              </Link>
            )}
            {can('conversations.use') && (
              <Button variant="primary" icon="message" onClick={() => navigate(`${base}/chat`, { state: { fresh: Date.now() } })}>
                Ask a question
              </Button>
            )}
          </>
        }
      />
      <div className="stack" style={{ gap: 24 }}>
        {!user.mfa_enabled && (workspace.role === 'owner' || workspace.role === 'admin') && (
          <Alert tone="warning">
            Protect this workspace: turn on two-factor authentication. It's required for sensitive actions such as
            deleting documents. <Link to={`${base}/profile#security`}>Set up 2FA</Link>
          </Alert>
        )}
        <div className="stat-grid">
          {stats.map((s) => (
            <Link key={s.label} to={s.to} className="stat card-link" style={{ gap: 4 }}>
              <span className="stat__label">{s.label}</span>
              <span className="stat__value">{s.value ?? '–'}</span>
              <span className="stat__hint">{s.hint}</span>
            </Link>
          ))}
        </div>

        {failed.data && failed.data.total > 0 && (
          <Alert>
            {failed.data.total} {failed.data.total === 1 ? 'document' : 'documents'} failed to process:{' '}
            {failed.data.items.map((d, i) => (
              <span key={d.id}>
                {i > 0 && ', '}
                <Link to={`${base}/documents/${d.id}`}>{d.title}</Link>
              </span>
            ))}
            .
          </Alert>
        )}

        <div className="grid-2">
          <section className="panel" aria-labelledby="recent-docs">
            <header className="panel__header">
              <h2 id="recent-docs">Recent documents</h2>
              <Link to={`${base}/documents`}>View all</Link>
            </header>
            {recent.isLoading ? (
              <div className="panel__body">
                <Loading lines={4} />
              </div>
            ) : recent.error ? (
              <div className="panel__body">
                <ErrorState error={recent.error} onRetry={() => void recent.refetch()} />
              </div>
            ) : !recent.data?.items.length ? (
              <div className="panel__body">
                <EmptyState
                  icon="upload"
                  title="No documents yet"
                  description="Upload a contract, policy or report to get started."
                  action={can('documents.write') && <Link className="btn btn--primary btn--md" to={`${base}/upload`}>Upload</Link>}
                />
              </div>
            ) : (
              <ul className="list">
                {recent.data.items.map((d) => (
                  <li key={d.id} className="list-row">
                    <span className="file-icon" aria-hidden="true">
                      <Icon name="file" size={16} />
                    </span>
                    <div className="list-row__main">
                      <Link to={`${base}/documents/${d.id}`} className="list-row__title truncate">
                        {d.title}
                      </Link>
                      <span className="list-row__meta">
                        {formatBytes(d.size_bytes)} · {formatRelative(d.created_at)}
                      </span>
                    </div>
                    <StatusBadge status={d.status} />
                  </li>
                ))}
              </ul>
            )}
          </section>

          {can('conversations.use') && (
            <section className="panel" aria-labelledby="recent-conv">
              <header className="panel__header">
                <h2 id="recent-conv">Recent conversations</h2>
                <Link to={`${base}/conversations`}>View all</Link>
              </header>
              {conversations.isLoading ? (
                <div className="panel__body">
                  <Loading lines={4} />
                </div>
              ) : !conversations.data?.items.length ? (
                <div className="panel__body">
                  <EmptyState
                    icon="message"
                    title="No conversations yet"
                    description="Ask a question and get an answer with a citation for every claim."
                    action={<Link className="btn btn--primary btn--md" to={`${base}/chat`}>Ask a question</Link>}
                  />
                </div>
              ) : (
                <ul className="list">
                  {conversations.data.items.map((c) => (
                    <li key={c.id} className="list-row">
                      <span className="file-icon" aria-hidden="true">
                        <Icon name="message" size={16} />
                      </span>
                      <div className="list-row__main">
                        <Link to={`${base}/chat/${c.id}`} className="list-row__title truncate">
                          {c.title}
                        </Link>
                        <span className="list-row__meta">{formatRelative(c.last_message_at)}</span>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          )}
        </div>
      </div>
    </>
  )
}
