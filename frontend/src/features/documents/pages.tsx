import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'

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
  Menu,
  PageHeader,
  Pagination,
  Select,
  Spinner,
  TextArea,
  TextInput,
  useConfirm,
  useToast,
  type MenuItem,
} from '../../components/ui'
import { errorMessage } from '../../lib/api/errors'
import type { Collection, DocumentItem, DocumentStatus, ExtractedPage } from '../../lib/api/types'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { useUser } from '../auth/AuthProvider'
import {
  ACCEPT,
  SUPPORTED_EXTENSIONS,
  addToCollection,
  allPages,
  archiveDocument,
  createCollection,
  deleteCollection,
  deleteDocument,
  documentKeys,
  downloadUrl,
  getDocument,
  listDocuments,
  listVersions,
  processingStatus,
  removeFromCollection,
  renameDocument,
  reprocessDocument,
  restoreDocument,
  updateCollection,
  uploadVersion,
  type DocumentFilters,
} from './api'
import { FORMAT_LABEL, STATUS_LABEL, STATUS_TONE, describeWarning, formatBytes, formatDate, formatDateTime, formatRelative } from './format'
import { findExcerpt } from './highlight'
import { useVoice } from '../voice/VoiceContext'
import { ReadingControls, type ReadingPosition } from '../voice/ReadingMode'
import { useCollections } from './hooks'
import { StepUpDialog } from './StepUpDialog'
import { useUploads } from './UploadManager'
import { startReindex } from '../saas/api'

const POLL_MS = 3000
const isBusy = (status: DocumentStatus) => status === 'pending' || status === 'processing'

function useTitle(title: string) {
  useEffect(() => {
    document.title = `${title} · DocuMind`
  }, [title])
}

export function ReindexBadge() {
  return (
    <Badge tone="warning">
      <span title="Indexed with a different AI model. Search by meaning can't find it until it is re-indexed.">
        Needs re-indexing
      </span>
    </Badge>
  )
}

/** Documents indexed with an older embedding model: say so, and offer to fix it. */
function ReindexBanner({ count }: { count: number }) {
  const { workspace, base, can } = useWorkspace()
  const queryClient = useQueryClient()
  const toast = useToast()
  const reindex = useMutation({
    mutationFn: () => startReindex(workspace.id),
    onSuccess: async (r) => {
      toast.success(r.queued === 1 ? 'Re-indexing 1 document.' : `Re-indexing ${r.queued} documents.`)
      await queryClient.invalidateQueries({ queryKey: documentKeys.all(workspace.id) })
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  const subject = count === 1 ? '1 document was' : `${count} documents were`
  return (
    <div id="reindex" className="reindex-banner">
      <Alert tone="warning">
        <p>
          <strong>{subject} indexed with a different AI model.</strong> Search and answers can only match{' '}
          {count === 1 ? 'it' : 'them'} by exact words, not by meaning, until {count === 1 ? "it's" : "they're"}{' '}
          re-indexed.
        </p>
        {can('ai_providers.manage') ? (
          <div className="row" style={{ marginTop: 8 }}>
            <Button size="sm" icon="refresh" loading={reindex.isPending} onClick={() => reindex.mutate()}>
              Re-index all
            </Button>
            <Link to={`${base}/ai-providers`}>AI provider settings</Link>
          </div>
        ) : (
          <p className="muted">Ask an owner or admin to re-index them, or use Reprocess on each document.</p>
        )}
      </Alert>
    </div>
  )
}

export function StatusBadge({ status }: { status: DocumentStatus }) {
  return (
    <Badge tone={STATUS_TONE[status]}>
      {isBusy(status) && <Spinner size={10} />}
      {STATUS_LABEL[status]}
    </Badge>
  )
}

// ------------------------------------------------------------ shared actions

/** Rename, archive/restore, reprocess and 2FA-protected delete for one document. */
function useDocumentActions(onDeleted?: () => void) {
  const { workspace, can } = useWorkspace()
  const user = useUser()
  const queryClient = useQueryClient()
  const toast = useToast()
  const confirm = useConfirm()
  const [renaming, setRenaming] = useState<DocumentItem | null>(null)
  const [deleting, setDeleting] = useState<DocumentItem | null>(null)
  const [filing, setFiling] = useState<DocumentItem | null>(null)
  const ws = workspace.id

  const refresh = () => queryClient.invalidateQueries({ queryKey: documentKeys.all(ws) })
  const run = async (action: () => Promise<unknown>, success: string) => {
    try {
      await action()
      await refresh()
      toast.success(success)
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  const canArchive = (doc: DocumentItem) =>
    can('documents.archive_any') || (can('documents.archive_own') && doc.uploaded_by_user_id === user.id)

  const items = (doc: DocumentItem, extra: MenuItem[] = []): (MenuItem | 'separator')[] => {
    const list: (MenuItem | 'separator')[] = [...extra]
    if (can('documents.write') && !doc.archived_at) {
      list.push({ label: 'Rename', icon: 'edit', onSelect: () => setRenaming(doc) })
      list.push({ label: 'Add to collection', icon: 'layers', onSelect: () => setFiling(doc) })
      if (doc.status === 'failed' || doc.status === 'ready') {
        list.push({
          label: 'Process again',
          icon: 'refresh',
          onSelect: () => void run(() => reprocessDocument(ws, doc.id), 'Queued for processing.'),
        })
      }
    }
    if (canArchive(doc)) {
      list.push(
        doc.archived_at
          ? { label: 'Restore', icon: 'restore', onSelect: () => void run(() => restoreDocument(ws, doc.id), 'Document restored.') }
          : {
              label: 'Archive',
              icon: 'archive',
              onSelect: async () => {
                const ok = await confirm({
                  title: `Archive "${doc.title}"?`,
                  description: 'Archived documents are read-only and are no longer used to answer questions. You can restore them later.',
                  confirmLabel: 'Archive',
                })
                if (ok) await run(() => archiveDocument(ws, doc.id), 'Document archived.')
              },
            },
      )
    }
    if (can('documents.delete')) {
      list.push('separator', { label: 'Delete permanently…', icon: 'trash', danger: true, onSelect: () => setDeleting(doc) })
    }
    return list
  }

  const dialogs = (
    <>
      {renaming && (
        <RenameDialog
          doc={renaming}
          onClose={() => setRenaming(null)}
          onSave={async (title) => {
            await renameDocument(ws, renaming.id, title)
            await refresh()
            toast.success('Document renamed.')
          }}
        />
      )}
      {filing && <AddToCollectionDialog doc={filing} onClose={() => setFiling(null)} />}
      <StepUpDialog
        open={deleting !== null}
        title={`Delete "${deleting?.title ?? ''}" permanently?`}
        description="The file, every version and its search index are deleted. Answers that cited it keep their quotes. This can't be undone."
        actionLabel="Delete permanently"
        onClose={() => setDeleting(null)}
        onVerified={async (token) => {
          if (!deleting) return
          await deleteDocument(ws, deleting.id, token)
          await refresh()
          toast.success('Document deleted.')
          onDeleted?.()
        }}
      />
    </>
  )
  return { items, dialogs }
}

function RenameDialog({ doc, onClose, onSave }: { doc: DocumentItem; onClose: () => void; onSave: (title: string) => Promise<void> }) {
  const [title, setTitle] = useState(doc.title)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    try {
      await onSave(title.trim())
      onClose()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog open title="Rename document" onClose={onClose} size="sm">
      <form className="form" onSubmit={submit}>
        {error && <Alert>{error}</Alert>}
        <Field label="Title">
          <TextInput value={title} onChange={(e) => setTitle(e.target.value)} maxLength={255} autoFocus required />
        </Field>
        <div className="form__actions">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" loading={busy} disabled={!title.trim()}>
            Save
          </Button>
        </div>
      </form>
    </Dialog>
  )
}

function AddToCollectionDialog({ doc, onClose }: { doc: DocumentItem; onClose: () => void }) {
  const { workspace } = useWorkspace()
  const queryClient = useQueryClient()
  const toast = useToast()
  const { data: collections, isLoading } = useCollections()
  const [selected, setSelected] = useState<Set<string>>(new Set(doc.collection_ids))
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState(false)

  const save = async () => {
    setBusy(true)
    try {
      const before = new Set(doc.collection_ids)
      let target = new Set(selected)
      if (newName.trim()) {
        const created = await createCollection(workspace.id, newName.trim())
        target = new Set([...target, created.id])
      }
      for (const id of target) if (!before.has(id)) await addToCollection(workspace.id, id, doc.id)
      for (const id of before) if (!target.has(id)) await removeFromCollection(workspace.id, id, doc.id)
      await queryClient.invalidateQueries({ queryKey: documentKeys.all(workspace.id) })
      await queryClient.invalidateQueries({ queryKey: documentKeys.collections(workspace.id) })
      toast.success('Collections updated.')
      onClose()
    } catch (err) {
      toast.error(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      open
      title="Collections"
      description={`Choose collections for "${doc.title}".`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={busy} onClick={() => void save()}>
            Save
          </Button>
        </>
      }
    >
      <div className="stack">
        {isLoading ? (
          <Loading lines={2} />
        ) : collections && collections.length > 0 ? (
          <ul className="picker-list" aria-label="Collections">
            {collections.map((c) => (
              <li key={c.id}>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={selected.has(c.id)}
                    onChange={(e) => {
                      const next = new Set(selected)
                      if (e.target.checked) next.add(c.id)
                      else next.delete(c.id)
                      setSelected(next)
                    }}
                  />
                  <span>
                    {c.name} <span className="muted">· {c.document_count} documents</span>
                  </span>
                </label>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No collections yet.</p>
        )}
        <Field label="Or create a new collection">
          <TextInput value={newName} onChange={(e) => setNewName(e.target.value)} maxLength={100} placeholder="e.g. Vendor contracts" />
        </Field>
      </div>
    </Dialog>
  )
}

// ---------------------------------------------------------------- library

export function DocumentLibraryPage() {
  useTitle('Documents')
  const { workspace, base, can } = useWorkspace()
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const { data: collections } = useCollections()
  const filters: DocumentFilters = {
    q: params.get('q') ?? '',
    status: (params.get('status') as DocumentStatus | null) ?? '',
    archived: (params.get('archived') as DocumentFilters['archived']) ?? 'exclude',
    collectionId: params.get('collection') ?? '',
    offset: Number(params.get('offset') ?? 0) || 0,
    limit: 25,
  }
  const [search, setSearch] = useState(filters.q ?? '')
  const [syncedQ, setSyncedQ] = useState(filters.q ?? '')
  if ((filters.q ?? '') !== syncedQ) {
    setSyncedQ(filters.q ?? '')
    setSearch(filters.q ?? '')
  }

  const query = useQuery({
    queryKey: documentKeys.list(workspace.id, filters),
    queryFn: () => listDocuments(workspace.id, filters),
    placeholderData: keepPreviousData,
    refetchInterval: (q) => (q.state.data?.items.some((d) => isBusy(d.status)) ? POLL_MS : false),
  })
  const { items: actions, dialogs } = useDocumentActions()

  const update = (change: Record<string, string>) => {
    const next = new URLSearchParams(params)
    for (const [key, value] of Object.entries(change)) {
      if (value) next.set(key, value)
      else next.delete(key)
    }
    if (!('offset' in change)) next.delete('offset')
    setParams(next, { replace: true })
  }

  const filtered = Boolean(filters.q || filters.status || filters.collectionId || filters.archived !== 'exclude')
  const collectionName = (id: string) => collections?.find((c) => c.id === id)?.name

  return (
    <>
      <PageHeader
        title="Documents"
        description="Everything in this workspace. Ready documents are searchable and used for answers."
        actions={
          can('documents.write') && (
            <Link className="btn btn--primary btn--md" to={`${base}/upload`}>
              <Icon name="upload" size={17} /> Upload
            </Link>
          )
        }
      />
      <form
        className="toolbar"
        role="search"
        aria-label="Filter documents"
        onSubmit={(e) => {
          e.preventDefault()
          update({ q: search.trim() })
        }}
      >
        <div className="toolbar__grow">
          <Field label="Search">
            <TextInput type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Title or filename" />
          </Field>
        </div>
        <Field label="Status">
          <Select value={filters.status} onChange={(e) => update({ status: e.target.value })}>
            <option value="">Any status</option>
            <option value="ready">Ready</option>
            <option value="processing">Processing</option>
            <option value="pending">Queued</option>
            <option value="failed">Failed</option>
          </Select>
        </Field>
        <Field label="Collection">
          <Select value={filters.collectionId} onChange={(e) => update({ collection: e.target.value })}>
            <option value="">All collections</option>
            {collections?.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Show">
          <Select value={filters.archived} onChange={(e) => update({ archived: e.target.value === 'exclude' ? '' : e.target.value })}>
            <option value="exclude">Active</option>
            <option value="only">Archived</option>
            <option value="include">All</option>
          </Select>
        </Field>
        <Button type="submit" icon="search">
          Search
        </Button>
      </form>

      {query.isLoading ? (
        <Loading label="Loading documents" lines={6} />
      ) : query.error ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : query.data && query.data.items.length === 0 ? (
        filtered ? (
          <EmptyState
            icon="search"
            title="No documents match"
            description="Try other words or clear the filters."
            action={<Button onClick={() => setParams({}, { replace: true })}>Clear filters</Button>}
          />
        ) : (
          <EmptyState
            icon="upload"
            title="No documents yet"
            description={`Upload PDFs, Word files, text or Markdown (${SUPPORTED_EXTENSIONS.join(', ')}). DocuMind indexes them so you can ask questions with cited answers.`}
            action={
              can('documents.write') && (
                <Link className="btn btn--primary btn--md" to={`${base}/upload`}>
                  Upload documents
                </Link>
              )
            }
          />
        )
      ) : (
        query.data && (
          <>
            {query.data.needs_reindex_total > 0 && <ReindexBanner count={query.data.needs_reindex_total} />}
            <div className="table-wrap">
              <table className="table">
                <caption className="sr-only">Documents, {query.data.total} in total</caption>
                <thead>
                  <tr>
                    <th scope="col">Name</th>
                    <th scope="col">Status</th>
                    <th scope="col">Size</th>
                    <th scope="col">Pages</th>
                    <th scope="col">Updated</th>
                    <th scope="col">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {query.data.items.map((doc) => (
                    <tr key={doc.id}>
                      <td>
                        <div className="table__primary">
                          <span className="file-icon" aria-hidden="true">
                            <Icon name="file" size={16} />
                          </span>
                          <div className="stack stack--sm" style={{ gap: 2, minWidth: 0 }}>
                            <Link to={`${base}/documents/${doc.id}`} className="truncate">
                              {doc.title}
                            </Link>
                            <span className="table__sub truncate">
                              {FORMAT_LABEL[doc.format] ?? doc.format} · {doc.original_filename}
                              {doc.collection_ids.length > 0 &&
                                ` · ${doc.collection_ids.map(collectionName).filter(Boolean).join(', ')}`}
                            </span>
                          </div>
                        </div>
                      </td>
                      <td>
                        <div className="row">
                          <StatusBadge status={doc.status} />
                          {doc.needs_reindex && <ReindexBadge />}
                          {doc.archived_at && <Badge tone="warning">Archived</Badge>}
                        </div>
                      </td>
                      <td>{formatBytes(doc.size_bytes)}</td>
                      <td>{doc.page_count ?? '—'}</td>
                      <td title={formatDateTime(doc.updated_at)}>{formatRelative(doc.updated_at)}</td>
                      <td className="table__actions">
                        <Menu
                          label={`Actions for ${doc.title}`}
                          trigger={<Icon name="more" />}
                          triggerClassName="btn btn--ghost btn--sm btn--icon"
                          items={actions(doc, [
                            { label: 'Open', icon: 'eye', onSelect: () => navigate(`${base}/documents/${doc.id}`) },
                            ...(doc.status === 'ready' && !doc.archived_at && can('conversations.use')
                              ? [{ label: 'Ask about this', icon: 'message' as const, onSelect: () => navigate(`${base}/chat?documents=${doc.id}`) }]
                              : []),
                          ])}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              offset={filters.offset ?? 0}
              limit={filters.limit ?? 25}
              total={query.data.total}
              onChange={(offset) => update({ offset: String(offset) })}
              label="documents"
            />
          </>
        )
      )}
      {dialogs}
    </>
  )
}

// ------------------------------------------------------------------ upload

export function UploadPage() {
  useTitle('Upload')
  const { base, can } = useWorkspace()
  const { enqueue, items } = useUploads()
  const { data: collections } = useCollections()
  const [collectionId, setCollectionId] = useState('')
  const [dragging, setDragging] = useState(false)
  const input = useRef<HTMLInputElement>(null)

  if (!can('documents.write')) {
    return <ErrorState title="You can't upload here" error={new Error('Your role in this workspace can read documents but not upload them.')} />
  }

  const add = (files: FileList | null) => {
    if (files?.length) enqueue(Array.from(files), { collectionId: collectionId || undefined })
  }

  return (
    <>
      <PageHeader
        title="Upload documents"
        description="Files are scanned, text is extracted (with OCR for scanned PDFs) and indexed. Large files take a minute."
        back={
          <Link to={`${base}/documents`} className="back-link">
            <Icon name="arrowLeft" size={14} /> Documents
          </Link>
        }
      />
      <div className="stack">
        <div
          className={`dropzone${dragging ? ' dropzone--active' : ''}`}
          onDragOver={(e) => {
            e.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDragging(false)
            add(e.dataTransfer.files)
          }}
        >
          <span className="dropzone__icon" aria-hidden="true">
            <Icon name="upload" size={22} />
          </span>
          <h2>Drop files here</h2>
          <p className="muted">PDF, Word (.docx), text and Markdown. Several at once is fine.</p>
          <input ref={input} type="file" multiple accept={ACCEPT} hidden onChange={(e) => { add(e.target.files); e.target.value = '' }} />
          <Button variant="primary" icon="file" onClick={() => input.current?.click()}>
            Choose files
          </Button>
        </div>
        {collections && collections.length > 0 && (
          <div style={{ maxWidth: 360 }}>
            <Field label="Add uploads to a collection" hint="Optional">
              <Select value={collectionId} onChange={(e) => setCollectionId(e.target.value)}>
                <option value="">No collection</option>
                {collections.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
        )}
        {items.length > 0 && (
          <section className="panel" aria-labelledby="uploads-heading">
            <header className="panel__header">
              <h2 id="uploads-heading">This session</h2>
              <Link to={`${base}/documents`}>View all documents</Link>
            </header>
            <ul className="list">
              {items.map((item) => (
                <li key={item.id} className="list-row">
                  <span className="file-icon" aria-hidden="true">
                    <Icon name={item.state === 'done' ? 'check' : item.state === 'failed' ? 'alert' : 'file'} size={16} />
                  </span>
                  <div className="list-row__main">
                    {item.documentId ? (
                      <Link className="list-row__title" to={`${base}/documents/${item.documentId}`}>
                        {item.name}
                      </Link>
                    ) : (
                      <span className="list-row__title">{item.name}</span>
                    )}
                    <span className="list-row__meta" style={item.state === 'failed' ? { color: 'var(--color-error)' } : undefined}>
                      {item.state === 'failed'
                        ? item.error
                        : item.state === 'done'
                          ? 'Uploaded. Processing has started.'
                          : `${formatBytes(item.size)} · ${item.state === 'queued' ? 'waiting' : `${Math.round(item.progress * 100)}%`}`}
                    </span>
                    {(item.state === 'queued' || item.state === 'uploading') && (
                      <progress className="progress" max={1} value={item.progress} aria-label={`Upload progress for ${item.name}`} />
                    )}
                  </div>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </>
  )
}

// ------------------------------------------------------------------ viewer

export function DocumentViewerPage() {
  const { documentId = '' } = useParams()
  const { workspace, base, can } = useWorkspace()
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const toast = useToast()
  const queryClient = useQueryClient()
  const ws = workspace.id

  const doc = useQuery({
    queryKey: documentKeys.detail(ws, documentId),
    queryFn: () => getDocument(ws, documentId),
    refetchInterval: (q) => (q.state.data && isBusy(q.state.data.status) ? POLL_MS : false),
  })
  useTitle(doc.data?.title ?? 'Document')
  const ready = doc.data?.status === 'ready'
  const pages = useQuery({
    queryKey: [...documentKeys.pages(ws, documentId), doc.data?.current_version_id, doc.data?.processed_at],
    queryFn: () => allPages(ws, documentId),
    enabled: ready,
  })
  const processing = useQuery({
    queryKey: documentKeys.processing(ws, documentId),
    queryFn: () => processingStatus(ws, documentId),
    enabled: Boolean(doc.data),
    refetchInterval: doc.data && isBusy(doc.data.status) ? POLL_MS : false,
  })
  const { items: actions, dialogs } = useDocumentActions(() => navigate(`${base}/documents`, { replace: true }))
  const [versionInput, setVersionInput] = useState<HTMLInputElement | null>(null)
  const voice = useVoice()
  const [listening, setListening] = useState(false)
  const [reading, setReading] = useState<ReadingPosition | null>(null)

  useEffect(() => {
    // When processing finishes, refresh lists that show this document.
    if (ready) void queryClient.invalidateQueries({ queryKey: documentKeys.processing(ws, documentId) })
  }, [ready, queryClient, ws, documentId])

  const download = async () => {
    try {
      const { url } = await downloadUrl(ws, documentId)
      window.location.assign(url)
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  const replaceVersion = async (file: File | undefined) => {
    if (!file) return
    try {
      await uploadVersion(ws, documentId, file)
      await queryClient.invalidateQueries({ queryKey: documentKeys.all(ws) })
      toast.success('New version uploaded. It is being processed.')
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  if (doc.isLoading) return <Loading label="Loading document" lines={8} />
  if (doc.error || !doc.data) return <ErrorState error={doc.error} onRetry={() => void doc.refetch()} />
  const d = doc.data
  const targetPage = Number(params.get('page') ?? 0) || null
  const excerpt = params.get('q') ?? ''

  return (
    <>
      <PageHeader
        back={
          <Link to={`${base}/documents`} className="back-link">
            <Icon name="arrowLeft" size={14} /> Documents
          </Link>
        }
        title={d.title}
        description={
          <span className="row">
            <StatusBadge status={d.status} />
            {d.needs_reindex && <ReindexBadge />}
            {d.archived_at && <Badge tone="warning">Archived</Badge>}
            <span>
              {FORMAT_LABEL[d.format]} · {formatBytes(d.size_bytes)}
              {d.page_count ? ` · ${d.page_count} pages` : ''}
            </span>
          </span>
        }
        actions={
          <>
            {ready && voice.ttsEngine && (
              <Button icon="headset" aria-pressed={listening} onClick={() => setListening((v) => !v)}>
                Listen
              </Button>
            )}
            {ready && !d.archived_at && can('conversations.use') && (
              <Button variant="primary" icon="message" onClick={() => navigate(`${base}/chat?documents=${d.id}`)}>
                Ask about this
              </Button>
            )}
            <Button icon="download" onClick={() => void download()}>
              Download
            </Button>
            <input ref={setVersionInput} type="file" accept={ACCEPT} hidden onChange={(e) => { void replaceVersion(e.target.files?.[0]); e.target.value = '' }} />
            <Menu
              label="More document actions"
              trigger={<Icon name="more" />}
              triggerClassName="btn btn--secondary btn--md btn--icon"
              items={actions(
                d,
                can('documents.write') && !d.archived_at
                  ? [{ label: 'Upload new version', icon: 'upload', onSelect: () => versionInput?.click() }]
                  : [],
              )}
            />
          </>
        }
      />
      <div className="viewer">
        <div className="viewer__pages">
          {d.status === 'failed' && (
            <Alert>
              Processing failed: {d.processing_error ?? 'unknown error'}.{' '}
              {can('documents.write') && (
                <button
                  type="button"
                  className="link-button"
                  onClick={() =>
                    void reprocessDocument(ws, d.id)
                      .then(() => queryClient.invalidateQueries({ queryKey: documentKeys.all(ws) }))
                      .then(() => toast.success('Queued for processing.'))
                      .catch((err: unknown) => toast.error(errorMessage(err)))
                  }
                >
                  Try again
                </button>
              )}
            </Alert>
          )}
          {isBusy(d.status) && (
            <div className="panel panel__body row" role="status">
              <Spinner /> {d.status === 'pending' ? 'Waiting to be processed…' : `Processing${processing.data?.jobs[0]?.stage ? ` (${processing.data.jobs[0].stage})` : ''}…`} This page updates by itself.
            </div>
          )}
          {ready &&
            (pages.isLoading ? (
              <Loading label="Loading pages" lines={10} />
            ) : pages.error ? (
              <ErrorState error={pages.error} onRetry={() => void pages.refetch()} />
            ) : (
              <>
                {listening && voice.ttsEngine && (
                  <ReadingControls
                    title={d.title}
                    documentId={d.id}
                    pages={pages.data ?? []}
                    position={reading}
                    onPosition={setReading}
                    onClose={() => {
                      setListening(false)
                      setReading(null)
                    }}
                  />
                )}
                <PageList
                  pages={pages.data ?? []}
                  targetPage={targetPage}
                  excerpt={excerpt}
                  reading={listening ? reading : null}
                  onJump={(page) => setParams({ page: String(page) }, { replace: true })}
                />
              </>
            ))}
        </div>
        <aside className="viewer__side" aria-label="Document details">
          <section className="panel">
            <header className="panel__header">
              <h2>Details</h2>
            </header>
            <div className="panel__body">
              <dl className="meta-list">
                <dt>File</dt>
                <dd>{d.original_filename}</dd>
                <dt>Uploaded</dt>
                <dd>{formatDateTime(d.created_at)}</dd>
                <dt>Processed</dt>
                <dd>{formatDateTime(d.processed_at)}</dd>
                <dt>Passages</dt>
                <dd>{d.chunk_count ?? '—'}</dd>
                <dt>Versions</dt>
                <dd>{d.version_count}</dd>
              </dl>
            </div>
          </section>
          {processing.data && processing.data.extraction_warnings.length > 0 && (
            <Alert tone="warning">
              <strong>Extraction notes</strong>
              <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                {processing.data.extraction_warnings.map((w) => (
                  <li key={w}>{describeWarning(w)}</li>
                ))}
              </ul>
            </Alert>
          )}
          <DocumentCollections doc={d} />
          <Versions documentId={d.id} />
        </aside>
      </div>
      {dialogs}
    </>
  )
}

function PageList({
  pages,
  targetPage,
  excerpt,
  reading = null,
  onJump,
}: {
  pages: ExtractedPage[]
  targetPage: number | null
  excerpt: string
  reading?: ReadingPosition | null
  onJump: (page: number) => void
}) {
  // Keep the sentence being read in view.
  useEffect(() => {
    if (!reading) return
    document.querySelector('mark.reading-highlight')?.scrollIntoView({ block: 'center' })
  }, [reading])

  const [jump, setJump] = useState('')
  const refs = useRef(new Map<number, HTMLElement>())
  const numbered = pages.some((p) => p.page_number !== null)

  useEffect(() => {
    if (!targetPage) return
    const node = refs.current.get(targetPage)
    if (node) {
      node.scrollIntoView({ block: 'start' })
      node.focus({ preventScroll: true })
    }
  }, [targetPage, pages])

  if (pages.length === 0) {
    return <EmptyState icon="file" title="No text found" description="This document has no extractable text." />
  }

  return (
    <>
      {numbered && pages.length > 1 && (
        <form
          className="page-jump"
          onSubmit={(e) => {
            e.preventDefault()
            const n = Number(jump)
            if (n >= 1) onJump(n)
          }}
        >
          <label htmlFor="page-jump" className="muted">
            Go to page
          </label>
          <TextInput id="page-jump" type="number" min={1} max={pages.length} value={jump} onChange={(e) => setJump(e.target.value)} style={{ width: 90 }} />
          <Button type="submit" size="sm">
            Go
          </Button>
          <span className="muted">of {pages.length}</span>
        </form>
      )}
      {pages.map((page, pageIndex) => {
        const number = page.page_number ?? page.position + 1
        const isTarget = targetPage === number || (!numbered && targetPage === null && Boolean(excerpt))
        const readingRange =
          reading && reading.pageIndex === pageIndex ? findExcerpt(page.text, reading.sentence) : null
        const range = readingRange ?? (isTarget && excerpt ? findExcerpt(page.text, excerpt) : null)
        const markClass = readingRange ? 'reading-highlight' : 'cite-highlight'
        return (
          <article
            key={page.position}
            className={`page-card${isTarget ? ' page-card--target' : ''}`}
            ref={(node) => {
              if (node) refs.current.set(number, node)
            }}
            tabIndex={-1}
            aria-label={page.page_number !== null ? `Page ${page.page_number}` : 'Document text'}
          >
            <header className="page-card__header">
              <span>{page.page_number !== null ? `Page ${page.page_number}` : 'Text'}</span>
              <span className="row">
                {page.headings[0] && <span className="truncate">{page.headings[0]}</span>}
                {page.ocr && <Badge>OCR</Badge>}
              </span>
            </header>
            <div className="page-card__text">
              {range ? (
                <>
                  {page.text.slice(0, range[0])}
                  <mark className={markClass}>{page.text.slice(range[0], range[1])}</mark>
                  {page.text.slice(range[1])}
                </>
              ) : (
                page.text
              )}
            </div>
          </article>
        )
      })}
    </>
  )
}

function DocumentCollections({ doc }: { doc: DocumentItem }) {
  const { base } = useWorkspace()
  const { data: collections } = useCollections()
  const names = (collections ?? []).filter((c) => doc.collection_ids.includes(c.id))
  return (
    <section className="panel">
      <header className="panel__header">
        <h2>Collections</h2>
      </header>
      <div className="panel__body">
        {names.length === 0 ? (
          <p className="muted">Not in any collection.</p>
        ) : (
          <div className="row">
            {names.map((c) => (
              <Link key={c.id} to={`${base}/collections/${c.id}`} className="source-chip">
                <Icon name="layers" size={13} /> {c.name}
              </Link>
            ))}
          </div>
        )}
      </div>
    </section>
  )
}

function Versions({ documentId }: { documentId: string }) {
  const { workspace } = useWorkspace()
  const { data } = useQuery({
    queryKey: documentKeys.versions(workspace.id, documentId),
    queryFn: () => listVersions(workspace.id, documentId),
  })
  if (!data || data.length < 2) return null
  return (
    <section className="panel">
      <header className="panel__header">
        <h2>Versions</h2>
      </header>
      <ul className="list">
        {data.map((v) => (
          <li key={v.id} className="list-row">
            <div className="list-row__main">
              <span className="list-row__title">
                v{v.version_number} {v.is_current && <Badge tone="accent">Current</Badge>}
              </span>
              <span className="list-row__meta">
                {v.original_filename} · {formatBytes(v.size_bytes)} · {formatDate(v.created_at)}
              </span>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

// ------------------------------------------------------------- collections

function CollectionDialog({
  collection,
  onClose,
}: {
  collection?: Collection
  onClose: (created?: Collection) => void
}) {
  const { workspace } = useWorkspace()
  const queryClient = useQueryClient()
  const toast = useToast()
  const [name, setName] = useState(collection?.name ?? '')
  const [description, setDescription] = useState(collection?.description ?? '')
  const mutation = useMutation({
    mutationFn: () =>
      collection
        ? updateCollection(workspace.id, collection.id, { name: name.trim(), description: description.trim() || null })
        : createCollection(workspace.id, name.trim(), description.trim()),
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: documentKeys.collections(workspace.id) })
      toast.success(collection ? 'Collection updated.' : 'Collection created.')
      onClose(saved)
    },
  })
  return (
    <Dialog open title={collection ? 'Edit collection' : 'New collection'} onClose={() => onClose()} size="sm">
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault()
          mutation.mutate()
        }}
      >
        {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
        <Field label="Name">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} maxLength={100} autoFocus required />
        </Field>
        <Field label="Description" hint="Optional">
          <TextArea value={description} onChange={(e) => setDescription(e.target.value)} maxLength={500} rows={3} />
        </Field>
        <div className="form__actions">
          <Button onClick={() => onClose()}>Cancel</Button>
          <Button type="submit" variant="primary" loading={mutation.isPending} disabled={!name.trim()}>
            {collection ? 'Save' : 'Create'}
          </Button>
        </div>
      </form>
    </Dialog>
  )
}

export function CollectionsPage() {
  useTitle('Collections')
  const { base, can } = useWorkspace()
  const navigate = useNavigate()
  const { data, error, isLoading, refetch } = useCollections()
  const [creating, setCreating] = useState(false)

  let body: ReactNode
  if (isLoading) body = <Loading label="Loading collections" />
  else if (error) body = <ErrorState error={error} onRetry={() => void refetch()} />
  else if (!data?.length)
    body = (
      <EmptyState
        icon="layers"
        title="No collections yet"
        description="Group related documents, such as all contracts with one vendor, and ask questions across just that set."
        action={can('documents.write') && <Button variant="primary" icon="plus" onClick={() => setCreating(true)}>New collection</Button>}
      />
    )
  else
    body = (
      <div className="card-grid">
        {data.map((c) => (
          <Link key={c.id} to={`${base}/collections/${c.id}`} className="card-link">
            <span className="file-icon" aria-hidden="true">
              <Icon name="layers" size={16} />
            </span>
            <h2>{c.name}</h2>
            {c.description && <p className="muted">{c.description}</p>}
            <span className="muted" style={{ fontSize: '0.82rem' }}>
              {c.document_count} {c.document_count === 1 ? 'document' : 'documents'} · updated {formatRelative(c.updated_at)}
            </span>
          </Link>
        ))}
      </div>
    )

  return (
    <>
      <PageHeader
        title="Collections"
        description="Named groups of documents. A document can be in several collections."
        actions={can('documents.write') && <Button variant="primary" icon="plus" onClick={() => setCreating(true)}>New collection</Button>}
      />
      {body}
      {creating && (
        <CollectionDialog
          onClose={(created) => {
            setCreating(false)
            if (created) navigate(`${base}/collections/${created.id}`)
          }}
        />
      )}
    </>
  )
}

export function CollectionDetailPage() {
  const { collectionId = '' } = useParams()
  const { workspace, base, can } = useWorkspace()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()
  const confirm = useConfirm()
  const { data: collections, isLoading, error, refetch } = useCollections()
  const collection = collections?.find((c) => c.id === collectionId)
  useTitle(collection?.name ?? 'Collection')
  const [editing, setEditing] = useState(false)
  const filters = useMemo<DocumentFilters>(() => ({ collectionId, limit: 100, archived: 'include' }), [collectionId])
  const docs = useQuery({
    queryKey: documentKeys.list(workspace.id, filters),
    queryFn: () => listDocuments(workspace.id, filters),
    enabled: Boolean(collection),
  })

  if (isLoading) return <Loading label="Loading collection" />
  if (error) return <ErrorState error={error} onRetry={() => void refetch()} />
  if (!collection) return <ErrorState title="Collection not found" error={new Error('It may have been deleted.')} />

  const remove = async (doc: DocumentItem) => {
    try {
      await removeFromCollection(workspace.id, collection.id, doc.id)
      await queryClient.invalidateQueries({ queryKey: documentKeys.all(workspace.id) })
      await queryClient.invalidateQueries({ queryKey: documentKeys.collections(workspace.id) })
      toast.success(`Removed "${doc.title}" from the collection.`)
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  const destroy = async () => {
    const ok = await confirm({
      title: `Delete collection "${collection.name}"?`,
      description: 'The documents stay in the workspace; only the grouping is removed.',
      confirmLabel: 'Delete collection',
      danger: true,
    })
    if (!ok) return
    try {
      await deleteCollection(workspace.id, collection.id)
      await queryClient.invalidateQueries({ queryKey: documentKeys.collections(workspace.id) })
      toast.success('Collection deleted.')
      navigate(`${base}/collections`, { replace: true })
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  return (
    <>
      <PageHeader
        back={
          <Link to={`${base}/collections`} className="back-link">
            <Icon name="arrowLeft" size={14} /> Collections
          </Link>
        }
        title={collection.name}
        description={collection.description ?? `${collection.document_count} documents`}
        actions={
          <>
            {can('conversations.use') && collection.document_count > 0 && (
              <Button variant="primary" icon="message" onClick={() => navigate(`${base}/chat?collections=${collection.id}`)}>
                Ask this collection
              </Button>
            )}
            {can('documents.write') && (
              <Menu
                label="Collection actions"
                trigger={<Icon name="more" />}
                triggerClassName="btn btn--secondary btn--md btn--icon"
                items={[
                  { label: 'Edit', icon: 'edit', onSelect: () => setEditing(true) },
                  { label: 'Delete collection', icon: 'trash', danger: true, onSelect: () => void destroy() },
                ]}
              />
            )}
          </>
        }
      />
      {docs.isLoading ? (
        <Loading label="Loading documents" />
      ) : docs.error ? (
        <ErrorState error={docs.error} onRetry={() => void docs.refetch()} />
      ) : !docs.data?.items.length ? (
        <EmptyState
          icon="file"
          title="This collection is empty"
          description="Add documents from the document list (… → Add to collection) or upload straight into it."
          action={can('documents.write') && <Link className="btn btn--primary btn--md" to={`${base}/upload`}>Upload documents</Link>}
        />
      ) : (
        <div className="panel">
          <ul className="list">
            {docs.data.items.map((doc) => (
              <li key={doc.id} className="list-row">
                <span className="file-icon" aria-hidden="true">
                  <Icon name="file" size={16} />
                </span>
                <div className="list-row__main">
                  <Link to={`${base}/documents/${doc.id}`} className="list-row__title truncate">
                    {doc.title}
                  </Link>
                  <span className="list-row__meta">
                    {FORMAT_LABEL[doc.format]} · {formatBytes(doc.size_bytes)} · {STATUS_LABEL[doc.status]}
                    {doc.archived_at ? ' · archived' : ''}
                  </span>
                </div>
                {can('documents.write') && (
                  <Button size="sm" variant="ghost" icon="x" label={`Remove ${doc.title} from collection`} onClick={() => void remove(doc)} />
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {editing && <CollectionDialog collection={collection} onClose={() => setEditing(false)} />}
    </>
  )
}
