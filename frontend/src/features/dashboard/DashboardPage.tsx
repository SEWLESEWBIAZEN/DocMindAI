import { useQuery } from '@tanstack/react-query'
import { useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router'

import { Alert, Button, EmptyState, ErrorState, Icon, Loading } from '../../components/ui'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { ROLE_LABELS } from '../../lib/workspace/permissions'
import { useUser } from '../auth/AuthProvider'
import { conversationKeys, listConversations } from '../chat/api'
import { documentKeys, listDocuments } from '../documents/api'
import { formatBytes, formatRelative } from '../documents/format'
import { FileTile } from '../documents/FileTile'
import { useCollections } from '../documents/hooks'
import { StatusBadge } from '../documents/pages'
import { GettingStarted, LoadSamplesButton } from '../onboarding/samples'
import { apiClient } from '../../lib/api/client'

const MFA_OFFER_KEY = 'documind.mfa-offer-dismissed'

/** Offered to everyone without 2FA; optional unless the workspace requires it (ADR 0026). */
function MfaOffer() {
  const user = useUser()
  const { workspace, base } = useWorkspace()
  const [dismissed, setDismissed] = useState(() => {
    try {
      return window.localStorage.getItem(MFA_OFFER_KEY) === user.id
    } catch {
      return false
    }
  })
  // A workspace that requires 2FA shows its own reminder instead.
  if (user.mfa_enabled || dismissed || workspace.mfa_enrollment_deadline) return null
  const dismiss = () => {
    try {
      window.localStorage.setItem(MFA_OFFER_KEY, user.id)
    } catch {
      // Not remembered in private windows; it just shows again next time.
    }
    setDismissed(true)
  }
  return (
    <div className="mfa-banner">
      <Alert tone="info">
        Protect your account with two-factor authentication: you'll enter a code from an authenticator app when you
        sign in. <Link to={`${base}/profile`}>Set it up</Link>{' '}
        <button type="button" className="link-button" onClick={dismiss}>
          Not now
        </button>
      </Alert>
    </div>
  )
}

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
  const members = useQuery({
    queryKey: ['ws', ws, 'members', 'count'],
    queryFn: async () => (await apiClient.get<unknown[]>(`/workspaces/${ws}/members`)).data.length,
    enabled: workspace.kind === 'organization' && can('members.read'),
  })

  const [question, setQuestion] = useState('')
  const askNow = (e: FormEvent) => {
    e.preventDefault()
    const q = question.trim()
    if (!q) return
    navigate(`${base}/chat`, { state: { fresh: Date.now(), ask: q } })
  }
  const collectionName = (ids: string[]) => (collections.data ?? []).find((c) => ids.includes(c.id))?.name
  const actions = [
    { label: 'Summarize a document', icon: 'sparkles' as const, to: `${base}/documents`, show: can('documents.read') },
    { label: 'Compare documents', icon: 'layers' as const, to: `${base}/extract`, show: can('documents.read') },
    { label: 'Extract information', icon: 'flask' as const, to: `${base}/extract`, show: can('documents.read') },
    { label: 'Find deadlines', icon: 'clock' as const, to: `${base}/deadlines`, show: can('documents.read') },
  ].filter((a) => a.show)

  return (
    <>
      <MfaOffer />
      <section className="home-hero" aria-labelledby="home-greeting">
        <h1 id="home-greeting" className="home-hero__title">
          {greeting()}, {user.display_name.split(' ')[0]}
        </h1>
        <p className="home-hero__lead">
          Ask a question, find information, or analyze your documents.
          <span className="home-hero__context">
            {' '}
            {workspace.name} · {workspace.kind === 'personal' ? 'Personal workspace' : 'Organization'} · {ROLE_LABELS[workspace.role]}
          </span>
        </p>
        {can('conversations.use') && (
          <form className="home-ask" role="search" aria-label="Ask your documents" onSubmit={askNow}>
            <Icon name="search" size={18} />
            <label htmlFor="home-ask" className="sr-only">
              Ask anything about your documents
            </label>
            <input
              id="home-ask"
              type="text"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="Ask anything about your documents…"
              autoComplete="off"
            />
            {can('documents.write') && (
              <Link to={`${base}/upload`} className="home-ask__attach" aria-label="Upload documents" title="Upload documents">
                <Icon name="upload" size={17} />
              </Link>
            )}
            <Button type="submit" variant="primary" icon="send" label="Ask" disabled={!question.trim()} />
          </form>
        )}
        {actions.length > 0 && (
          <ul className="home-actions" aria-label="Quick actions">
            {actions.map((a) => (
              <li key={a.label}>
                <Link to={a.to} className="home-chip">
                  <Icon name={a.icon} size={14} /> {a.label}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
      <div className="stack" style={{ gap: 24 }}>
        <GettingStarted
          documents={recent.data?.total}
          conversations={conversations.data?.total}
          members={members.data}
          mfaEnabled={user.mfa_enabled}
        />
        {!user.mfa_enabled && (workspace.role === 'owner' || workspace.role === 'admin') && (
          <Alert tone="warning">
            Protect this workspace: turn on two-factor authentication. It's required for sensitive actions such as
            deleting documents. <Link to={`${base}/profile#security`}>Set up 2FA</Link>
          </Alert>
        )}
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

        <div className="grid-2 home-lists">
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
                  description="Upload a contract, policy or report, or try DocuMind with our sample documents."
                  action={
                    can('documents.write') && (
                      <span className="row">
                        <Link className="btn btn--primary btn--md" to={`${base}/upload`}>
                          Upload
                        </Link>
                        <LoadSamplesButton />
                      </span>
                    )
                  }
                />
              </div>
            ) : (
              <ul className="list">
                {recent.data.items.map((d) => (
                  <li key={d.id} className="list-row">
                    <FileTile format={d.format} />
                    <div className="list-row__main">
                      <Link to={`${base}/documents/${d.id}`} className="list-row__title truncate">
                        {d.title}
                      </Link>
                      <span className="list-row__meta">
                        Updated {formatRelative(d.updated_at ?? d.created_at)}
                        {collectionName(d.collection_ids) ? ` · ${collectionName(d.collection_ids)}` : ` · ${formatBytes(d.size_bytes)}`}
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
                <h2 id="recent-conv">Continue your work</h2>
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
                      <span className="file-icon file-icon--chat" aria-hidden="true">
                        <Icon name="message" size={16} />
                      </span>
                      <div className="list-row__main">
                        <Link to={`${base}/chat/${c.id}`} className="list-row__title truncate">
                          {c.title}
                        </Link>
                        <span className="list-row__meta">You asked {formatRelative(c.last_message_at)}</span>
                      </div>
                      <Icon name="chevronRight" size={16} />
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
