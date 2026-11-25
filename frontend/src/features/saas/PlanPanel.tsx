import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'

import { Badge, ErrorState, Loading } from '../../components/ui'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { CAPABILITY_LABELS, formatQuota, getPlan, planKey, quotaPercent, type Quota } from './api'

const MONTH = new Intl.DateTimeFormat(undefined, { month: 'long', day: 'numeric' })

function QuotaRow({ quota }: { quota: Quota }) {
  const percent = quotaPercent(quota)
  const id = `quota-${quota.metric}`
  const used = formatQuota(quota.metric, quota.used)
  const limit = quota.limit === null || quota.limit === undefined ? 'Unlimited' : formatQuota(quota.metric, quota.limit)
  const tone = percent === null ? 'ok' : percent >= 100 ? 'error' : percent >= 80 ? 'warn' : 'ok'
  return (
    <li className="quota">
      <div className="quota__label">
        <span id={id}>{quota.label[0].toUpperCase() + quota.label.slice(1)}</span>
        <span className="quota__value">
          {used} of {limit}
          {quota.exceeded && (
            <Badge tone="danger">
              Limit reached
            </Badge>
          )}
        </span>
      </div>
      {percent !== null && (
        <meter
          className={`quota__meter quota__meter--${tone}`}
          aria-labelledby={id}
          min={0}
          max={100}
          low={80}
          high={99}
          optimum={0}
          value={percent}
        >
          {percent}%
        </meter>
      )}
    </li>
  )
}

/** The workspace's plan, monthly quotas and metered usage (ADR 0020). */
export function PlanPanel() {
  const { workspace, base, can } = useWorkspace()
  const query = useQuery({ queryKey: planKey(workspace.id), queryFn: () => getPlan(workspace.id) })
  if (query.isLoading) return <Loading label="Loading plan" lines={3} />
  if (query.error || !query.data) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />
  const p = query.data
  const monthly = p.quotas.filter((q) => !['max_documents', 'storage_bytes', 'max_members'].includes(q.metric))
  const totals = p.quotas.filter((q) => ['max_documents', 'storage_bytes', 'max_members'].includes(q.metric))
  const ownKey = p.usage.filter((u) => u.billed_to === 'workspace_key')

  return (
    <section className="panel" aria-labelledby="plan-heading">
      <header className="panel__header">
        <div>
          <h2 id="plan-heading">
            {p.plan_name} plan{p.has_overrides ? ' (custom limits)' : ''}
          </h2>
          <p className="panel__description">
            {p.plan_description} Monthly limits reset on {MONTH.format(new Date(p.period_end))}. Only usage on DocuMind's
            AI key counts; usage on your own key is shown but never limited here.
          </p>
        </div>
        <div className="row">
          {can('billing.manage') && (
            <Link className="btn btn--primary btn--md" to={`${base}/billing`}>
              Change plan
            </Link>
          )}
          {can('ai_providers.manage') && (
            <Link className="btn btn--secondary btn--md" to={`${base}/ai-providers`}>
              AI providers
            </Link>
          )}
        </div>
      </header>
      <div className="panel__body plan-grid">
        <div>
          <h3 className="section-title">This month</h3>
          <ul className="quota-list">
            {monthly.map((q) => (
              <QuotaRow key={q.metric} quota={q} />
            ))}
          </ul>
        </div>
        <div>
          <h3 className="section-title">Workspace totals</h3>
          <ul className="quota-list">
            {totals.map((q) => (
              <QuotaRow key={q.metric} quota={q} />
            ))}
          </ul>
        </div>
      </div>
      {ownKey.length > 0 && (
        <div className="panel__body">
          <h3 className="section-title">On your own AI key this month</h3>
          <ul className="inline-list">
            {ownKey.map((u) => (
              <li key={u.capability}>
                {CAPABILITY_LABELS[u.capability] ?? u.capability}:{' '}
                {u.audio_seconds > 0
                  ? formatQuota('audio_seconds', u.audio_seconds)
                  : `${new Intl.NumberFormat().format(u.input_tokens + u.output_tokens)} tokens`}{' '}
                ({u.calls} calls)
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
