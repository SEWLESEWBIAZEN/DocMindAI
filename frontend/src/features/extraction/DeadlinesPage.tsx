/** Tracked deadlines with email reminders and a calendar file (ADR 0034). */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link } from 'react-router'

import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Icon,
  Loading,
  PageHeader,
  TextArea,
  TextInput,
  useConfirm,
  useToast,
} from '../../components/ui'
import { errorMessage } from '../../lib/api/errors'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { formatDate } from '../documents/format'
import {
  createDeadline,
  deleteDeadline,
  downloadCalendar,
  extractionKeys,
  listDeadlines,
  reminderLabel,
  updateDeadline,
  type Deadline,
} from './api'

const CHOICES = [30, 14, 7, 1, 0]
const DAY = 86_400_000

function daysUntil(iso: string, today: Date): number {
  const [y, m, d] = iso.split('-').map(Number)
  const due = Date.UTC(y, m - 1, d)
  const now = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate())
  return Math.round((due - now) / DAY)
}

function dueLabel(days: number): string {
  if (days < 0) return days === -1 ? 'Overdue by 1 day' : `Overdue by ${-days} days`
  if (days === 0) return 'Due today'
  if (days === 1) return 'Due tomorrow'
  return `Due in ${days} days`
}

interface Draft {
  id?: string
  title: string
  due_date: string
  notes: string
  remind: number[]
}

function EditDialog({ draft, onClose }: { draft: Draft | null; onClose: () => void }) {
  const { workspace } = useWorkspace()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [value, setValue] = useState<Draft | null>(draft)
  const [synced, setSynced] = useState(draft)
  if (draft !== synced) {
    setSynced(draft)
    setValue(draft)
  }
  const save = useMutation({
    mutationFn: async () => {
      const v = value!
      const body = { title: v.title.trim(), due_date: v.due_date, notes: v.notes.trim() || null, remind_days_before: v.remind }
      return v.id ? updateDeadline(workspace.id, v.id, body) : createDeadline(workspace.id, body)
    },
    onSuccess: async () => {
      toast.success(value?.id ? 'Deadline updated.' : 'Deadline added.')
      await queryClient.invalidateQueries({ queryKey: extractionKeys.allDeadlines(workspace.id) })
      onClose()
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  return (
    <Dialog
      open={draft !== null}
      title={draft?.id ? 'Edit deadline' : 'Add a deadline'}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.isPending} disabled={!value?.title.trim() || !value?.due_date} onClick={() => save.mutate()}>
            Save
          </Button>
        </>
      }
    >
      {value && (
        <div className="stack">
          <Field label="Title">
            <TextInput value={value.title} maxLength={300} onChange={(e) => setValue({ ...value, title: e.target.value })} />
          </Field>
          <Field label="Due date">
            <TextInput type="date" value={value.due_date} onChange={(e) => setValue({ ...value, due_date: e.target.value })} />
          </Field>
          <Field label="Notes (optional)">
            <TextArea rows={3} value={value.notes} maxLength={2000} onChange={(e) => setValue({ ...value, notes: e.target.value })} />
          </Field>
          <fieldset className="fieldset">
            <legend>Email reminders</legend>
            <div className="row">
              {CHOICES.map((d) => (
                <label key={d} className="checkbox">
                  <input
                    type="checkbox"
                    checked={value.remind.includes(d)}
                    onChange={(e) =>
                      setValue({ ...value, remind: e.target.checked ? [...value.remind, d] : value.remind.filter((x) => x !== d) })
                    }
                  />
                  {reminderLabel(d)}
                </label>
              ))}
            </div>
          </fieldset>
        </div>
      )}
    </Dialog>
  )
}

function DeadlineRow({ d, today, onEdit }: { d: Deadline; today: Date; onEdit: (d: Deadline) => void }) {
  const { workspace, base, can } = useWorkspace()
  const toast = useToast()
  const confirm = useConfirm()
  const queryClient = useQueryClient()
  const refresh = () => queryClient.invalidateQueries({ queryKey: extractionKeys.allDeadlines(workspace.id) })
  const toggle = useMutation({
    mutationFn: () => updateDeadline(workspace.id, d.id, { status: d.status === 'open' ? 'done' : 'open' }),
    onSuccess: async () => {
      toast.success(d.status === 'open' ? 'Marked as done.' : 'Reopened.')
      await refresh()
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  const remove = useMutation({
    mutationFn: () => deleteDeadline(workspace.id, d.id),
    onSuccess: async () => {
      toast.success('Deadline deleted.')
      await refresh()
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  const days = daysUntil(d.due_date, today)
  const tone = d.status === 'done' ? 'neutral' : days < 0 ? 'danger' : days <= 7 ? 'warning' : 'neutral'
  const source = d.source as { page_start?: number } | null
  return (
    <li className="list-row deadline-row">
      <div className="list-row__main">
        <span className="list-row__title">{d.title}</span>
        <span className="list-row__meta">
          {formatDate(d.due_date)}
          {d.document_id && d.document_title && (
            <>
              {' · '}
              <Link to={`${base}/documents/${d.document_id}${source?.page_start ? `?page=${source.page_start}` : ''}`}>{d.document_title}</Link>
            </>
          )}
          {d.status === 'open' && d.remind_days_before.length > 0 && ` · Reminders: ${d.remind_days_before.map(reminderLabel).join(', ').toLowerCase()}`}
        </span>
        {d.notes && <span className="list-row__meta">{d.notes}</span>}
      </div>
      <Badge tone={tone}>{d.status === 'done' ? 'Done' : dueLabel(days)}</Badge>
      {can('documents.write') && (
        <span className="row">
          <Button size="sm" icon={d.status === 'open' ? 'check' : 'restore'} loading={toggle.isPending} onClick={() => toggle.mutate()}>
            {d.status === 'open' ? 'Done' : 'Reopen'}
          </Button>
          <Button size="sm" variant="ghost" icon="edit" label={`Edit ${d.title}`} onClick={() => onEdit(d)} />
          <Button
            size="sm"
            variant="ghost"
            icon="trash"
            label={`Delete ${d.title}`}
            onClick={async () => {
              if (await confirm({ title: 'Delete this deadline?', confirmLabel: 'Delete', danger: true })) remove.mutate()
            }}
          />
        </span>
      )}
    </li>
  )
}

export function DeadlinesPage() {
  const { workspace, base, can } = useWorkspace()
  const toast = useToast()
  const [status, setStatus] = useState<'open' | 'done'>('open')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [today] = useState(() => new Date())
  useEffect(() => {
    document.title = 'Deadlines · DocuMind'
  }, [])
  const query = useQuery({
    queryKey: extractionKeys.deadlines(workspace.id, status),
    queryFn: () => listDeadlines(workspace.id, status),
  })
  const rows = query.data ?? []
  const groups =
    status === 'done'
      ? [{ label: 'Done', items: rows }]
      : [
          { label: 'Overdue', items: rows.filter((d) => daysUntil(d.due_date, today) < 0) },
          { label: 'Next 7 days', items: rows.filter((d) => { const n = daysUntil(d.due_date, today); return n >= 0 && n <= 7 }) },
          { label: 'Later', items: rows.filter((d) => daysUntil(d.due_date, today) > 7) },
        ]
  const edit = (d: Deadline) =>
    setDraft({ id: d.id, title: d.title, due_date: d.due_date, notes: d.notes ?? '', remind: d.remind_days_before })

  return (
    <>
      <PageHeader
        title="Deadlines"
        description="Dates from your documents that DocuMind reminds you about by email."
        actions={
          <>
            <Button icon="download" onClick={() => void downloadCalendar(workspace.id).catch((e) => toast.error(errorMessage(e)))}>
              Calendar file (.ics)
            </Button>
            {can('documents.write') && (
              <Button variant="primary" icon="plus" onClick={() => setDraft({ title: '', due_date: '', notes: '', remind: [7, 1, 0] })}>
                Add deadline
              </Button>
            )}
          </>
        }
      />
      <div className="stack">
        <div className="choice-row results-view" role="radiogroup" aria-label="Show">
          {(['open', 'done'] as const).map((s) => (
            <label key={s} className={`choice${status === s ? ' choice--selected' : ''}`}>
              <input type="radio" name="deadline-status" checked={status === s} onChange={() => setStatus(s)} />
              <span>{s === 'open' ? 'Upcoming' : 'Done'}</span>
            </label>
          ))}
        </div>
        {query.isLoading ? (
          <Loading lines={4} />
        ) : query.error ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState
            icon="clock"
            title={status === 'open' ? 'No upcoming deadlines' : 'Nothing done yet'}
            description="Run an Obligations and deadlines extraction, then choose Track on a date."
            action={
              <Link className="btn btn--secondary btn--md" to={`${base}/extract`}>
                Extract from documents
              </Link>
            }
          />
        ) : (
          groups
            .filter((g) => g.items.length)
            .map((g) => (
              <section key={g.label} className="panel" aria-label={g.label}>
                <header className="panel__header">
                  <h2>
                    {g.label} <span className="muted">({g.items.length})</span>
                  </h2>
                </header>
                <ul className="list">
                  {g.items.map((d) => (
                    <DeadlineRow key={d.id} d={d} today={today} onEdit={edit} />
                  ))}
                </ul>
              </section>
            ))
        )}
        <p className="muted">
          <Icon name="info" size={14} /> Reminders go to the person who added the deadline, at the start of the day (UTC).
        </p>
      </div>
      <EditDialog draft={draft} onClose={() => setDraft(null)} />
    </>
  )
}
