import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { Badge, EmptyState, ErrorState, Field, Icon, Loading, PageHeader, Select } from '../../components/ui'
import { isStatus } from '../../lib/api/errors'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { getAnswerQuality, type AnswerQuality, type FeedbackReason } from '../chat/api'
import { REASON_LABELS } from '../chat/feedbackReasons'
import { formatNumber, formatRelative } from '../documents/format'

/** How members rate answers (ADR 0038). Admins only; questions appear only when shared. */
export function AnswerQualityPage() {
  const { workspace, can } = useWorkspace()
  const [days, setDays] = useState(30)
  useEffect(() => {
    document.title = 'Answer quality · DocuMind'
  }, [])
  const query = useQuery({
    queryKey: ['ws', workspace.id, 'answer-quality', days],
    queryFn: () => getAnswerQuality(workspace.id, days),
    enabled: can('usage.read'),
  })

  const header = (
    <PageHeader
      title="Answer quality"
      description="How people rate DocuMind's answers. Conversations stay private: questions appear here only when someone chose to share them."
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
        <PageHeader title="Answer quality" />
        <EmptyState icon="lock" title="Only admins can see answer quality" description="Ask an admin or owner of this workspace." />
      </>
    )
  }
  if (query.isLoading) return <>{header}<Loading label="Loading answer quality" lines={6} /></>
  if (query.error || !query.data) return <>{header}<ErrorState error={query.error} onRetry={() => void query.refetch()} /></>
  return (
    <>
      {header}
      <Summary q={query.data} />
    </>
  )
}

const pct = (part: number, whole: number) => (whole ? `${Math.round((part / whole) * 100)}%` : '–')

function Summary({ q }: { q: AnswerQuality }) {
  const reasons = (Object.entries(q.reasons) as [FeedbackReason, number][]).sort((a, b) => b[1] - a[1])
  const top = Math.max(1, ...reasons.map(([, n]) => n))
  return (
    <div className="stack" style={{ gap: 24 }}>
      <div className="stat-grid">
        <div className="stat">
          <span className="stat__label">Rated helpful</span>
          <span className="stat__value">{pct(q.up, q.rated)}</span>
          <span className="stat__hint">
            {formatNumber(q.up)} up · {formatNumber(q.down)} down
          </span>
        </div>
        <div className="stat">
          <span className="stat__label">Answers</span>
          <span className="stat__value">{formatNumber(q.answers)}</span>
          <span className="stat__hint">last {q.days} days</span>
        </div>
        <div className="stat">
          <span className="stat__label">Rated</span>
          <span className="stat__value">{pct(q.rated, q.answers)}</span>
          <span className="stat__hint">{formatNumber(q.rated)} answers rated</span>
        </div>
        <div className="stat">
          <span className="stat__label">Not in documents</span>
          <span className="stat__value">{pct(q.insufficient_context, q.answers)}</span>
          <span className="stat__hint">{formatNumber(q.insufficient_context)} answers found nothing relevant</span>
        </div>
      </div>

      <div className="grid-2">
        <section className="panel" aria-labelledby="quality-reasons">
          <header className="panel__header">
            <h2 id="quality-reasons">Why answers were rated down</h2>
          </header>
          <div className="panel__body">
            {q.down === 0 ? (
              <p className="muted">No thumbs down in this period.</p>
            ) : (
              <ul className="bar-list">
                {reasons.map(([reason, n]) => (
                  <li key={reason}>
                    <span className="bar-list__label">{REASON_LABELS[reason] ?? reason}</span>
                    <span className="bar-list__track" aria-hidden="true">
                      <span className="bar-list__fill" style={{ width: `${(n / top) * 100}%` }} />
                    </span>
                    <span className="bar-list__value">{formatNumber(n)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>
        <section className="panel" aria-labelledby="quality-models">
          <header className="panel__header">
            <h2 id="quality-models">By model</h2>
          </header>
          {q.models.length === 0 ? (
            <div className="panel__body">
              <p className="muted">No ratings in this period.</p>
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Model</th>
                  <th scope="col">Helpful</th>
                  <th scope="col">Up</th>
                  <th scope="col">Down</th>
                </tr>
              </thead>
              <tbody>
                {q.models.map((m) => (
                  <tr key={m.model}>
                    <td>
                      <code>{m.model}</code>
                    </td>
                    <td>{pct(m.up, m.up + m.down)}</td>
                    <td>{formatNumber(m.up)}</td>
                    <td>{formatNumber(m.down)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>

      <section className="panel" aria-labelledby="quality-shared">
        <header className="panel__header">
          <div>
            <h2 id="quality-shared">Shared with admins</h2>
            <p className="panel__description">Answers people chose to share, newest first.</p>
          </div>
        </header>
        {q.shared.length === 0 ? (
          <div className="panel__body">
            <p className="muted">Nothing shared yet. When someone rates an answer down, they can choose to share it here.</p>
          </div>
        ) : (
          <ul className="list">
            {q.shared.map((f) => (
              <li key={f.message_id} className="feedback-item">
                <div className="row row--between">
                  <span className="row">
                    <Badge tone={f.rating === 'up' ? 'success' : 'danger'}>
                      <Icon name={f.rating === 'up' ? 'thumbsUp' : 'thumbsDown'} size={13} /> {f.rating === 'up' ? 'Helpful' : 'Not helpful'}
                    </Badge>
                    {f.reasons.map((r) => (
                      <Badge key={r}>{REASON_LABELS[r as FeedbackReason] ?? r}</Badge>
                    ))}
                  </span>
                  <span className="muted" style={{ fontSize: '0.8rem' }}>
                    {formatRelative(f.rated_at)}
                  </span>
                </div>
                <p className="feedback-item__q">
                  <strong>Q:</strong> {f.question}
                </p>
                <p className="feedback-item__a">{f.answer}</p>
                {f.comment && <blockquote className="feedback-item__comment">“{f.comment}”</blockquote>}
                <p className="muted" style={{ fontSize: '0.8rem' }}>
                  {f.citation_count} source{f.citation_count === 1 ? '' : 's'} cited
                  {f.answer_status === 'insufficient_context' && ' · said the documents don’t cover it'}
                  {f.model && ` · ${f.model}`}
                  {f.prompt_version && ` · prompt ${f.prompt_version}`}
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
