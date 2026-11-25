/** Structured extraction: start a run, see results with sources, compare, track dates (ADR 0034). */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
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
  PageHeader,
  Select,
  Spinner,
  TextArea,
  TextInput,
  useConfirm,
  useToast,
} from '../../components/ui'
import { errorMessage } from '../../lib/api/errors'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { documentKeys, listDocuments } from '../documents/api'
import { formatDate, formatRelative } from '../documents/format'
import { useCollections } from '../documents/hooks'
import {
  createDeadline,
  deleteRun,
  downloadCsv,
  ERROR_LABELS,
  extractionKeys,
  getRun,
  getTemplates,
  listRuns,
  startRun,
  TEMPLATE_ICONS,
  WARNING_LABELS,
  type CitedValue,
  type FieldSpec,
  type RunDetail,
  reminderLabel,
} from './api'

const STATUS: Record<string, { label: string; tone: 'neutral' | 'accent' | 'success' | 'danger' }> = {
  queued: { label: 'Queued', tone: 'neutral' },
  running: { label: 'Extracting', tone: 'accent' },
  completed: { label: 'Done', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
}
const BUSY = new Set(['queued', 'running'])
const REMINDER_CHOICES = [30, 14, 7, 1, 0]

function useTitle(title: string) {
  useEffect(() => {
    document.title = `${title} · DocuMind`
  }, [title])
}


// ----------------------------------------------------------------- start a run

function NewExtraction() {
  const { workspace, base } = useWorkspace()
  const navigate = useNavigate()
  const toast = useToast()
  const [params] = useSearchParams()
  const templates = useQuery({ queryKey: extractionKeys.templates(workspace.id), queryFn: () => getTemplates(workspace.id) })
  const docs = useQuery({
    queryKey: documentKeys.list(workspace.id, { status: 'ready', limit: 100 }),
    queryFn: () => listDocuments(workspace.id, { status: 'ready', limit: 100 }),
  })
  const { data: collections } = useCollections()
  const [template, setTemplate] = useState('obligations')
  const [source, setSource] = useState<'documents' | 'collection'>('documents')
  const [selected, setSelected] = useState<string[]>(() => params.get('documents')?.split(',').filter(Boolean) ?? [])
  const [collection, setCollection] = useState('')
  const [filter, setFilter] = useState('')
  const [title, setTitle] = useState('')
  const [mode, setMode] = useState<'fields' | 'items'>('fields')
  const [fields, setFields] = useState<FieldSpec[]>([{ label: '', description: '', type: 'text' }])

  const start = useMutation({
    mutationFn: () =>
      startRun(workspace.id, {
        template,
        title: title.trim() || null,
        document_ids: source === 'documents' ? selected : [],
        collection_id: source === 'collection' ? collection || null : null,
        ...(template === 'custom' ? { mode, fields: fields.filter((f) => f.label.trim()) } : {}),
      }),
    onSuccess: (run) => navigate(`${base}/extract/${run.id}`),
    onError: (err) => toast.error(errorMessage(err)),
  })

  const visible = (docs.data?.items ?? []).filter((d) => d.title.toLowerCase().includes(filter.trim().toLowerCase()))
  const ready = source === 'documents' ? selected.length > 0 : Boolean(collection)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (ready) start.mutate()
  }

  return (
    <section className="panel" aria-labelledby="new-extraction">
      <header className="panel__header">
        <div>
          <h2 id="new-extraction">New extraction</h2>
          <p className="panel__description">
            DocuMind reads each document and fills in a table. Every value links to the passage it came from.
          </p>
        </div>
      </header>
      <form className="panel__body stack" onSubmit={submit} noValidate>
        <fieldset className="fieldset">
          <legend>What to extract</legend>
          {templates.isLoading ? (
            <Loading lines={2} />
          ) : (
            <div className="choice-row" role="radiogroup" aria-label="Template">
              {[...(templates.data ?? []), { key: 'custom', name: 'Your own fields', description: 'Name the fields you need.' }].map((t) => (
                <label key={t.key} className={`choice${template === t.key ? ' choice--selected' : ''}`}>
                  <input type="radio" name="template" value={t.key} checked={template === t.key} onChange={() => setTemplate(t.key)} />
                  <span>
                    <Icon name={TEMPLATE_ICONS[t.key] ?? 'file'} size={15} /> {t.name}
                  </span>
                  <small>{t.description}</small>
                </label>
              ))}
            </div>
          )}
        </fieldset>

        {template === 'custom' && (
          <fieldset className="fieldset">
            <legend>Your fields</legend>
            <div className="choice-row" role="radiogroup" aria-label="Result shape">
              <label className={`choice${mode === 'fields' ? ' choice--selected' : ''}`}>
                <input type="radio" name="mode" checked={mode === 'fields'} onChange={() => setMode('fields')} />
                <span>One value per document</span>
                <small>For example a contract's start date. Good for comparing documents.</small>
              </label>
              <label className={`choice${mode === 'items' ? ' choice--selected' : ''}`}>
                <input type="radio" name="mode" checked={mode === 'items'} onChange={() => setMode('items')} />
                <span>A list of items</span>
                <small>For example every risk or every product mentioned.</small>
              </label>
            </div>
            <ol className="field-list">
              {fields.map((f, i) => (
                <li key={i} className="form-grid">
                  <Field label={`Field ${i + 1} name`}>
                    <TextInput value={f.label} maxLength={80} onChange={(e) => setFields(fields.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)))} />
                  </Field>
                  <Field label="Type">
                    <Select value={f.type ?? 'text'} onChange={(e) => setFields(fields.map((x, j) => (j === i ? { ...x, type: e.target.value as FieldSpec['type'] } : x)))}>
                      <option value="text">Text</option>
                      <option value="date">Date</option>
                      <option value="money">Amount</option>
                      <option value="number">Number</option>
                    </Select>
                  </Field>
                  <Field label="What it means (optional)">
                    <TextInput value={f.description ?? ''} maxLength={300} onChange={(e) => setFields(fields.map((x, j) => (j === i ? { ...x, description: e.target.value } : x)))} />
                  </Field>
                  {fields.length > 1 && (
                    <Button variant="ghost" size="sm" icon="trash" label={`Remove field ${i + 1}`} onClick={() => setFields(fields.filter((_, j) => j !== i))} />
                  )}
                </li>
              ))}
            </ol>
            {fields.length < 12 && (
              <Button size="sm" icon="plus" onClick={() => setFields([...fields, { label: '', description: '', type: 'text' }])}>
                Add field
              </Button>
            )}
          </fieldset>
        )}

        <fieldset className="fieldset">
          <legend>From which documents</legend>
          <div className="choice-row" role="radiogroup" aria-label="Documents">
            <label className={`choice${source === 'documents' ? ' choice--selected' : ''}`}>
              <input type="radio" name="source" checked={source === 'documents'} onChange={() => setSource('documents')} />
              <span>Choose documents</span>
            </label>
            <label className={`choice${source === 'collection' ? ' choice--selected' : ''}`}>
              <input type="radio" name="source" checked={source === 'collection'} onChange={() => setSource('collection')} />
              <span>A whole collection</span>
            </label>
          </div>
          {source === 'collection' ? (
            <Field label="Collection">
              <Select value={collection} onChange={(e) => setCollection(e.target.value)}>
                <option value="">Choose a collection</option>
                {collections?.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} ({c.document_count})
                  </option>
                ))}
              </Select>
            </Field>
          ) : docs.isLoading ? (
            <Loading lines={3} />
          ) : !docs.data?.items.length ? (
            <p className="muted">There are no ready documents yet.</p>
          ) : (
            <>
              <Field label="Filter documents">
                <TextInput type="search" value={filter} onChange={(e) => setFilter(e.target.value)} />
              </Field>
              <ul className="doc-picker" aria-label="Documents to extract from">
                {visible.map((d) => (
                  <li key={d.id}>
                    <label className="checkbox">
                      <input
                        type="checkbox"
                        checked={selected.includes(d.id)}
                        onChange={(e) => setSelected(e.target.checked ? [...selected, d.id] : selected.filter((x) => x !== d.id))}
                      />
                      {d.title}
                    </label>
                  </li>
                ))}
              </ul>
              <p className="muted" aria-live="polite">
                {selected.length} selected
              </p>
            </>
          )}
        </fieldset>

        <Field label="Name (optional)">
          <TextInput value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Supplier contracts Q3" />
        </Field>
        <div>
          <Button type="submit" variant="primary" icon="flask" loading={start.isPending} disabled={!ready}>
            Extract
          </Button>
        </div>
      </form>
    </section>
  )
}

export function ExtractionsPage() {
  useTitle('Extract')
  const { workspace, base, can } = useWorkspace()
  const runs = useQuery({
    queryKey: extractionKeys.runs(workspace.id),
    queryFn: () => listRuns(workspace.id),
    refetchInterval: (q) => (q.state.data?.some((r) => BUSY.has(r.status)) ? 3000 : false),
  })
  return (
    <>
      <PageHeader
        title="Extract"
        description="Turn documents into tables: obligations and deadlines, contract terms, invoice details or your own fields."
        actions={
          <Link className="btn btn--secondary btn--md" to={`${base}/deadlines`}>
            <Icon name="clock" size={17} /> Deadlines
          </Link>
        }
      />
      <div className="stack" style={{ gap: 24 }}>
        {can('conversations.use') && <NewExtraction />}
        <section className="panel" aria-labelledby="past-extractions">
          <header className="panel__header">
            <h2 id="past-extractions">Extractions</h2>
          </header>
          {runs.isLoading ? (
            <div className="panel__body">
              <Loading lines={3} />
            </div>
          ) : runs.error ? (
            <div className="panel__body">
              <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />
            </div>
          ) : !runs.data?.length ? (
            <div className="panel__body">
              <EmptyState icon="flask" title="No extractions yet" description="Start one above. Results stay here for the whole workspace." />
            </div>
          ) : (
            <ul className="list">
              {runs.data.map((r) => (
                <li key={r.id} className="list-row">
                  <span className="file-icon" aria-hidden="true">
                    <Icon name={TEMPLATE_ICONS[r.template_key] ?? 'flask'} size={16} />
                  </span>
                  <div className="list-row__main">
                    <Link to={`${base}/extract/${r.id}`} className="list-row__title truncate">
                      {r.title}
                    </Link>
                    <span className="list-row__meta">
                      {r.document_ids.length} {r.document_ids.length === 1 ? 'document' : 'documents'} · {formatRelative(r.created_at)}
                    </span>
                  </div>
                  <Badge tone={STATUS[r.status].tone}>
                    {BUSY.has(r.status) && <Spinner size={10} />}
                    {STATUS[r.status].label}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  )
}

// ------------------------------------------------------------------ one run

function SourceLink({ value, documentId }: { value: CitedValue; documentId: string }) {
  const { base } = useWorkspace()
  const params = new URLSearchParams()
  if (value.page_start) params.set('page', String(value.page_start))
  if (value.excerpt) params.set('q', value.value.slice(0, 120))
  return (
    <Link className="source-link" to={`${base}/documents/${documentId}?${params}`} title={value.excerpt ?? undefined}>
      <Icon name="quote" size={12} />
      {value.page_start ? `p. ${value.page_start}` : 'Source'}
      <span className="sr-only"> (open the passage)</span>
    </Link>
  )
}

interface TrackTarget {
  title: string
  date: string
  documentId: string
  value: CitedValue
}

function Cell({
  value,
  documentId,
  type,
  onTrack,
  canTrack,
}: {
  value: CitedValue | undefined
  documentId: string
  type: string | undefined
  onTrack: (date: string, value: CitedValue) => void
  canTrack: boolean
}) {
  if (!value) return <span className="muted" aria-label="Not found">—</span>
  return (
    <span className="cell-value">
      <span>{type === 'date' && value.date ? `${formatDate(value.date)}` : value.value}</span>
      {type === 'date' && value.date && value.value !== formatDate(value.date) && <span className="muted"> ({value.value})</span>}
      <span className="cell-value__actions">
        <SourceLink value={value} documentId={documentId} />
        {canTrack && value.date && (
          <button type="button" className="link-button" onClick={() => onTrack(value.date!, value)}>
            <Icon name="clock" size={12} /> Track
          </button>
        )}
      </span>
    </span>
  )
}

function TrackDialog({ target, onClose }: { target: TrackTarget | null; onClose: () => void }) {
  const { workspace, base } = useWorkspace()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [title, setTitle] = useState('')
  const [date, setDate] = useState('')
  const [reminders, setReminders] = useState<number[]>([7, 1, 0])
  const [synced, setSynced] = useState<TrackTarget | null>(null)
  if (target !== synced) {
    setSynced(target)
    if (target) {
      setTitle(target.title)
      setDate(target.date)
      setReminders([7, 1, 0])
    }
  }
  const save = useMutation({
    mutationFn: () =>
      createDeadline(workspace.id, {
        title: title.trim(),
        due_date: date,
        document_id: target!.documentId,
        source: target!.value,
        remind_days_before: reminders,
      }),
    onSuccess: async () => {
      toast.success('Deadline added. You will get reminders by email.')
      await queryClient.invalidateQueries({ queryKey: extractionKeys.allDeadlines(workspace.id) })
      onClose()
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  return (
    <Dialog
      open={target !== null}
      title="Track this deadline"
      description="DocuMind emails you before it is due. You can change it on the Deadlines page."
      onClose={onClose}
      footer={
        <>
          <Link className="btn btn--ghost btn--md" to={`${base}/deadlines`}>
            All deadlines
          </Link>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.isPending} disabled={!title.trim() || !date} onClick={() => save.mutate()}>
            Add deadline
          </Button>
        </>
      }
    >
      <div className="stack">
        <Field label="Title">
          <TextArea rows={2} value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field label="Due date">
          <TextInput type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        </Field>
        <fieldset className="fieldset">
          <legend>Remind me</legend>
          <div className="row">
            {REMINDER_CHOICES.map((d) => (
              <label key={d} className="checkbox">
                <input
                  type="checkbox"
                  checked={reminders.includes(d)}
                  onChange={(e) => setReminders(e.target.checked ? [...reminders, d] : reminders.filter((x) => x !== d))}
                />
                {reminderLabel(d)}
              </label>
            ))}
          </div>
        </fieldset>
      </div>
    </Dialog>
  )
}

function differs(values: (CitedValue | undefined)[]): boolean {
  const seen = new Set(values.map((v) => (v ? (v.date ?? v.value).toLowerCase().replace(/\W+/g, ' ').trim() : '')))
  return seen.size > 1
}

function Results({ run }: { run: RunDetail }) {
  const { can } = useWorkspace()
  const [view, setView] = useState<'compare' | 'table'>(run.mode === 'fields' && run.document_ids.length > 1 ? 'compare' : 'table')
  const [track, setTrack] = useState<TrackTarget | null>(null)
  const fields = run.fields
  const title = (id: string) => run.documents[id] ?? 'Deleted document'
  const canTrack = can('documents.write')
  const byDoc = useMemo(() => new Map(run.results.map((r) => [r.document_id, r])), [run.results])

  if (!run.results.length) {
    return run.status === 'completed' ? (
      <EmptyState icon="search" title="Nothing found" description="The documents don't state these fields. Try other fields or documents." />
    ) : null
  }
  const trackFor = (documentId: string, label: string, main?: CitedValue) => (date: string, value: CitedValue) =>
    setTrack({ title: main ? main.value.slice(0, 300) : `${label}: ${title(documentId)}`, date, documentId, value })

  return (
    <>
      {run.mode === 'fields' && run.document_ids.length > 1 && (
        <div className="choice-row results-view" role="radiogroup" aria-label="View">
          {(['compare', 'table'] as const).map((v) => (
            <label key={v} className={`choice${view === v ? ' choice--selected' : ''}`}>
              <input type="radio" name="view" checked={view === v} onChange={() => setView(v)} />
              <span>{v === 'compare' ? 'Compare side by side' : 'One row per document'}</span>
            </label>
          ))}
        </div>
      )}
      <div className="table-wrap">
        {run.mode === 'fields' && view === 'compare' ? (
          <table className="table results-table">
            <caption className="sr-only">Fields compared across documents</caption>
            <thead>
              <tr>
                <th scope="col">Field</th>
                {run.document_ids.map((id) => (
                  <th key={id} scope="col">
                    {title(id)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {fields.map((f) => {
                const values = run.document_ids.map((id) => byDoc.get(id)?.values[f.key!])
                const diff = differs(values)
                return (
                  <tr key={f.key} className={diff ? 'row--differs' : undefined}>
                    <th scope="row">
                      {f.label}
                      {diff && (
                        <>
                          {' '}
                          <Badge tone="warning">Differs</Badge>
                        </>
                      )}
                    </th>
                    {run.document_ids.map((id, i) => (
                      <td key={id}>
                        <Cell value={values[i]} documentId={id} type={f.type} canTrack={canTrack} onTrack={trackFor(id, f.label)} />
                      </td>
                    ))}
                  </tr>
                )
              })}
            </tbody>
          </table>
        ) : (
          <table className="table results-table">
            <caption className="sr-only">Extracted values</caption>
            <thead>
              <tr>
                <th scope="col">Document</th>
                {fields.map((f) => (
                  <th key={f.key} scope="col">
                    {f.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {run.results.map((r) => {
                const main = run.mode === 'items' ? r.values[fields[0].key!] : undefined
                return (
                  <tr key={r.id}>
                    <th scope="row">{title(r.document_id)}</th>
                    {fields.map((f) => (
                      <td key={f.key}>
                        <Cell value={r.values[f.key!]} documentId={r.document_id} type={f.type} canTrack={canTrack} onTrack={trackFor(r.document_id, f.label, main)} />
                      </td>
                    ))}
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
      <TrackDialog target={track} onClose={() => setTrack(null)} />
    </>
  )
}

export function ExtractionRunPage() {
  const { runId = '' } = useParams()
  const { workspace, base } = useWorkspace()
  const navigate = useNavigate()
  const toast = useToast()
  const confirm = useConfirm()
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: extractionKeys.run(workspace.id, runId),
    queryFn: () => getRun(workspace.id, runId),
    refetchInterval: (q) => (q.state.data && BUSY.has(q.state.data.status) ? 2000 : false),
  })
  useTitle(query.data?.title ?? 'Extraction')
  const remove = useMutation({
    mutationFn: () => deleteRun(workspace.id, runId),
    onSuccess: async () => {
      toast.success('Extraction deleted.')
      await queryClient.invalidateQueries({ queryKey: extractionKeys.runs(workspace.id) })
      navigate(`${base}/extract`)
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  const back = (
    <Link to={`${base}/extract`} className="back-link">
      <Icon name="arrowLeft" size={14} /> Extract
    </Link>
  )
  if (query.isLoading) return <><PageHeader back={back} title="Extraction" /><Loading lines={6} /></>
  if (query.error || !query.data) return <><PageHeader back={back} title="Extraction" /><ErrorState error={query.error} onRetry={() => void query.refetch()} /></>
  const run = query.data
  const warnings = Object.entries(run.warnings)
  return (
    <>
      <PageHeader
        back={back}
        title={run.title}
        description={
          <span className="row">
            <Badge tone={STATUS[run.status].tone}>
              {BUSY.has(run.status) && <Spinner size={10} />}
              {STATUS[run.status].label}
            </Badge>
            <span>
              {run.documents_done} of {run.document_ids.length} {run.document_ids.length === 1 ? 'document' : 'documents'} read ·{' '}
              {formatRelative(run.created_at)}
            </span>
          </span>
        }
        actions={
          <>
            {run.status === 'completed' && run.results.length > 0 && (
              <Button icon="download" onClick={() => void downloadCsv(workspace.id, run).catch((e) => toast.error(errorMessage(e)))}>
                Export CSV
              </Button>
            )}
            <Button
              variant="ghost"
              icon="trash"
              onClick={async () => {
                if (await confirm({ title: 'Delete this extraction?', description: 'The documents are not affected.', confirmLabel: 'Delete', danger: true }))
                  remove.mutate()
              }}
            >
              Delete
            </Button>
          </>
        }
      />
      <div className="stack">
        {run.status === 'failed' && <Alert>{ERROR_LABELS[run.error_code ?? ''] ?? 'The extraction failed. Try again.'}</Alert>}
        {BUSY.has(run.status) && (
          <p className="muted" role="status">
            Reading the documents. Results appear here as each one is done.
          </p>
        )}
        {warnings.length > 0 && (
          <Alert tone="warning">
            <ul className="plain-list">
              {warnings.map(([id, code]) => (
                <li key={id}>
                  <strong>{run.documents[id] ?? 'A document'}:</strong> {WARNING_LABELS[code] ?? code}
                </li>
              ))}
            </ul>
          </Alert>
        )}
        <Results run={run} />
        <p className="muted">
          Extracted by AI: check important values against the source before relying on them. Values without a source are never shown.
        </p>
      </div>
    </>
  )
}
