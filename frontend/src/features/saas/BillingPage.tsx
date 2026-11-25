/** Plans, subscription and payment (ADR 0032). Owners only (`billing.manage`). */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { Alert, Badge, Button, EmptyState, ErrorState, Icon, Loading, PageHeader, useConfirm, useToast } from '../../components/ui'
import { apiClient } from '../../lib/api/client'
import { errorMessage, isStatus } from '../../lib/api/errors'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { formatDate } from '../documents/format'
import { highlights, money, priceLine, type CatalogPlan, type Interval, type PlanCatalog } from '../landing/pricing/catalog'
import {
  billingKey,
  changePlan,
  getBilling,
  openPortal,
  planKey,
  startCheckout,
  type BillingOverview,
  type PaidPlan,
} from './api'

const STATUS: Record<string, { label: string; tone: 'success' | 'warning' | 'danger' | 'neutral' }> = {
  active: { label: 'Active', tone: 'success' },
  trialing: { label: 'Trial', tone: 'success' },
  past_due: { label: 'Payment due', tone: 'warning' },
  incomplete: { label: 'Awaiting payment', tone: 'warning' },
  canceled: { label: 'Cancelled', tone: 'neutral' },
  unpaid: { label: 'Unpaid', tone: 'danger' },
}
const LIVE = new Set(['active', 'trialing', 'past_due'])
const RANK: Record<string, number> = { free: 0, pro: 1, business: 2, enterprise: 3 }

const FEATURE_LINKS: Record<string, string> = {
  custom_storage: 'your own object storage',
  dedicated_database: 'a dedicated database',
  external_database: 'your own database',
  customer_workers: 'processing on your own servers',
}

function list(items: string[]): string {
  return items.length <= 1 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`
}

async function getCatalog(): Promise<PlanCatalog> {
  return (await apiClient.get<PlanCatalog>('/plans', { skipAuth: true })).data
}

function IntervalChoice({ value, onChange, months }: { value: Interval; onChange: (v: Interval) => void; months: number }) {
  return (
    <div className="choice-row billing-interval" role="radiogroup" aria-label="Billing period">
      {(['month', 'year'] as const).map((option) => (
        <label key={option} className={`choice${value === option ? ' choice--selected' : ''}`}>
          <input type="radio" name="billing-interval" value={option} checked={value === option} onChange={() => onChange(option)} />
          <span>{option === 'month' ? 'Monthly' : 'Yearly'}</span>
          {option === 'year' && <small>{12 - months} months free</small>}
        </label>
      ))}
    </div>
  )
}

export function BillingPage() {
  const { workspace, can } = useWorkspace()
  const [params, setParams] = useSearchParams()
  const queryClient = useQueryClient()
  const toast = useToast()
  const confirm = useConfirm()
  const wanted = params.get('plan')
  const returned = params.get('checkout')
  const [interval, setInterval] = useState<Interval>(params.get('interval') === 'month' ? 'month' : 'year')
  const [waiting, setWaiting] = useState(returned === 'success')

  useEffect(() => {
    document.title = 'Billing · DocuMind'
  }, [])

  const overview = useQuery({
    queryKey: billingKey(workspace.id),
    queryFn: () => getBilling(workspace.id),
    enabled: can('billing.manage'),
    // After Checkout, Stripe's webhook usually lands within seconds.
    refetchInterval: (q) => (waiting && !(q.state.data?.status && LIVE.has(q.state.data.status)) ? 2000 : false),
  })
  const catalog = useQuery({ queryKey: ['plans'], queryFn: getCatalog, staleTime: 5 * 60_000 })

  const live = Boolean(overview.data?.status && LIVE.has(overview.data.status))
  const confirmedPlan = waiting && live ? overview.data?.plan_name : undefined
  const announced = useRef(false)
  useEffect(() => {
    if (!confirmedPlan || announced.current) return
    announced.current = true
    toast.success(`You're on the ${confirmedPlan} plan. Thank you!`)
    void queryClient.invalidateQueries({ queryKey: planKey(workspace.id) })
  }, [confirmedPlan, queryClient, toast, workspace.id])
  useEffect(() => {
    if (!waiting) return
    const timer = window.setTimeout(() => setWaiting(false), 60_000)
    return () => window.clearTimeout(timer)
  }, [waiting])

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: billingKey(workspace.id) })
    await queryClient.invalidateQueries({ queryKey: planKey(workspace.id) })
  }
  const checkout = useMutation({
    mutationFn: (plan: PaidPlan) => startCheckout(workspace.id, plan, interval),
    onSuccess: (url) => window.location.assign(url),
    onError: (err) => toast.error(errorMessage(err)),
  })
  const change = useMutation({
    mutationFn: (plan: PaidPlan) => changePlan(workspace.id, plan, interval),
    onSuccess: async (o) => {
      toast.success(`Switched to ${o.plan_name}. The difference is prorated on your next invoice.`)
      await refresh()
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  const portal = useMutation({
    mutationFn: () => openPortal(workspace.id),
    onSuccess: (url) => window.location.assign(url),
    onError: (err) => toast.error(errorMessage(err)),
  })

  if (!can('billing.manage') || isStatus(overview.error, 403)) {
    return (
      <>
        <PageHeader title="Billing" />
        <EmptyState icon="lock" title="Only owners manage billing" description="Ask an owner of this workspace to change its plan." />
      </>
    )
  }
  if (overview.isLoading || catalog.isLoading) return <><PageHeader title="Billing" /><Loading label="Loading billing" /></>
  if (overview.error || !overview.data || catalog.error || !catalog.data) {
    return (
      <>
        <PageHeader title="Billing" />
        <ErrorState error={overview.error ?? catalog.error} onRetry={() => void Promise.all([overview.refetch(), catalog.refetch()])} />
      </>
    )
  }
  const o: BillingOverview = overview.data
  const plans: PlanCatalog = catalog.data
  const current = plans.plans.find((p) => p.key === o.plan_key)
  const contact = o.sales_email ? `mailto:${o.sales_email}?subject=${encodeURIComponent(`DocuMind plan for ${workspace.name}`)}` : null

  const choose = async (plan: CatalogPlan) => {
    const key = plan.key as PaidPlan
    if (!live) {
      checkout.mutate(key)
      return
    }
    const price = priceLine(plan, interval)
    const ok = await confirm({
      title: `Switch to ${plan.name}?`,
      description: `${price.amount} ${price.unit} for ${o.seats || o.members} member(s), ${
        interval === 'year' ? 'billed yearly' : 'billed monthly'
      }. The change applies now and the difference is prorated on your next invoice.`,
      confirmLabel: `Switch to ${plan.name}`,
    })
    if (ok) change.mutate(key)
  }

  const action = (plan: CatalogPlan) => {
    const isCurrent = plan.key === o.plan_key && (!live || o.interval === interval || plan.key === 'free')
    if (isCurrent) return <Badge tone="success">Current plan</Badge>
    const blocked = o.blocked_plans[plan.key]
    if (plan.key === 'free') {
      return live ? (
        <p className="muted billing-plan__hint">To move to Free, cancel in the billing portal. Nothing is deleted.</p>
      ) : null
    }
    if (plan.contact_sales || !plan.self_serve || !o.enabled || o.managed_by === 'contract') {
      return contact ? (
        <a className="btn btn--secondary btn--md" href={contact}>
          Contact sales
        </a>
      ) : (
        <p className="muted billing-plan__hint">Contact us to change your plan.</p>
      )
    }
    const higher = RANK[plan.key] > RANK[o.plan_key]
    const label = !live ? `Upgrade to ${plan.name}` : plan.key === o.plan_key ? `Switch to ${interval === 'year' ? 'yearly' : 'monthly'}` : `Switch to ${plan.name}`
    return (
      <>
        <Button
          variant={higher ? 'primary' : 'secondary'}
          loading={(checkout.isPending && checkout.variables === plan.key) || (change.isPending && change.variables === plan.key)}
          disabled={Boolean(blocked) || checkout.isPending || change.isPending}
          onClick={() => void choose(plan)}
          aria-describedby={blocked ? `blocked-${plan.key}` : undefined}
        >
          {label}
        </Button>
        {blocked && (
          <p id={`blocked-${plan.key}`} className="billing-plan__hint billing-plan__hint--warn">
            {blocked}
          </p>
        )}
      </>
    )
  }

  const perSeat = current && o.interval ? priceLine(current, o.interval as Interval) : null
  const seatPrice =
    current && o.interval
      ? (o.interval === 'year' ? current.annual_cents_per_member : current.monthly_cents_per_member)
      : null

  return (
    <>
      <PageHeader
        title="Billing"
        description="Your plan, members and payment. Paid plans are priced per member."
        actions={
          o.can_manage_payment && (
            <Button icon="external" loading={portal.isPending} onClick={() => portal.mutate()}>
              Payment & invoices
            </Button>
          )
        }
      />
      <div className="stack">
        {returned === 'success' && (
          <Alert tone={live ? 'success' : 'info'}>
            {live ? 'Payment received. Your new plan is active.' : 'Payment received. Your plan is being updated…'}{' '}
            <button type="button" className="link-button" onClick={() => setParams({}, { replace: true })}>
              Dismiss
            </button>
          </Alert>
        )}
        {returned === 'cancelled' && <Alert tone="info">Checkout was cancelled. You haven't been charged.</Alert>}
        {!o.enabled && (
          <Alert tone="info">
            Online payments aren't set up on this server.{' '}
            {contact ? <a href={contact}>Contact sales</a> : 'Contact your administrator'} to change your plan.
          </Alert>
        )}
        {o.managed_by === 'contract' && (
          <Alert tone="info">
            This workspace's plan is managed under a contract.{' '}
            {contact ? <a href={contact}>Contact sales</a> : 'Contact us'} to change it.
          </Alert>
        )}
        {o.status === 'past_due' && (
          <Alert tone="warning">
            The latest payment didn't go through. Stripe will retry; update your payment method to keep your plan.{' '}
            <button type="button" className="link-button" onClick={() => portal.mutate()}>
              Update payment method
            </button>
          </Alert>
        )}
        {o.cancel_at_period_end && o.current_period_end && (
          <Alert tone="info">
            Your subscription ends on {formatDate(o.current_period_end)}. After that the workspace moves to Free; nothing is
            deleted.
          </Alert>
        )}
        {o.over_plan.length > 0 && (
          <Alert tone="warning">
            This workspace uses {list(o.over_plan.map((f) => FEATURE_LINKS[f] ?? f))}, which the {o.plan_name} plan doesn't
            include. Everything keeps working, but you can't change that setup until you upgrade or{' '}
            <Link to={`/w/${workspace.id}/infrastructure`}>move the data back to DocuMind</Link>.
          </Alert>
        )}

        <section className="panel" aria-labelledby="current-plan">
          <header className="panel__header">
            <div>
              <h2 id="current-plan">
                {o.plan_name} plan {o.status && STATUS[o.status] && <Badge tone={STATUS[o.status].tone}>{STATUS[o.status].label}</Badge>}
              </h2>
              <p className="panel__description">{current?.description}</p>
            </div>
          </header>
          <dl className="panel__body billing-facts">
            <div>
              <dt>Members</dt>
              <dd>
                {o.members}
                {live && o.seats !== o.members && <span className="muted"> (billed for {o.seats}; updates within the hour)</span>}
              </dd>
            </div>
            {perSeat && seatPrice !== null && (
              <div>
                <dt>Price</dt>
                <dd>
                  {money(seatPrice)} per member / {o.interval === 'year' ? 'year' : 'month'}
                  <span className="muted"> · {money(seatPrice * Math.max(o.seats, 1))} in total</span>
                </dd>
              </div>
            )}
            {o.current_period_end && live && (
              <div>
                <dt>{o.cancel_at_period_end ? 'Ends' : 'Renews'}</dt>
                <dd>{formatDate(o.current_period_end)}</dd>
              </div>
            )}
            <div>
              <dt>Infrastructure</dt>
              <dd>
                {o.features_in_use.length ? list(o.features_in_use.map((f) => FEATURE_LINKS[f] ?? f)) : "DocuMind's database and storage"}
              </dd>
            </div>
          </dl>
        </section>

        <section className="panel" aria-labelledby="choose-plan" id="plans">
          <header className="panel__header">
            <div>
              <h2 id="choose-plan">{live ? 'Change plan' : 'Choose a plan'}</h2>
              <p className="panel__description">
                Prices in US dollars, excluding taxes. <a href="/pricing" target="_blank" rel="noreferrer">Compare every feature</a>
              </p>
            </div>
            <IntervalChoice value={interval} onChange={setInterval} months={plans.annual_months_charged} />
          </header>
          <ul className="panel__body billing-plans">
            {plans.plans.map((plan) => {
              const price = priceLine(plan, interval)
              return (
                <li
                  key={plan.key}
                  className={`billing-plan${plan.key === o.plan_key ? ' billing-plan--current' : ''}${plan.key === wanted ? ' billing-plan--wanted' : ''}`}
                  aria-labelledby={`billing-${plan.key}`}
                >
                  <h3 id={`billing-${plan.key}`}>{plan.name}</h3>
                  <p className="billing-plan__price">
                    <strong>{price.amount}</strong> <span className="muted">{price.unit}</span>
                  </p>
                  <p className="muted billing-plan__note">{price.note ?? ' '}</p>
                  <div className="billing-plan__action">{action(plan)}</div>
                  <ul className="billing-plan__list" aria-label={`${plan.name} includes`}>
                    {highlights(plan).map((item) => (
                      <li key={item}>
                        <Icon name="check" size={15} />
                        {item}
                      </li>
                    ))}
                  </ul>
                </li>
              )
            })}
          </ul>
        </section>
        <p className="muted">
          Payments are processed by Stripe; DocuMind never sees your card details. Members are counted automatically, and
          changes are prorated.
        </p>
      </div>
    </>
  )
}
