/** Plan catalog from GET /api/v1/plans (ADR 0032): shared by the pricing page and in-app billing. */
import { useEffect, useState } from 'react'

import type { Schemas } from '../../../lib/api/types'

export type PlanCatalog = Schemas['PlanCatalogResponse']
export type CatalogPlan = Schemas['PlanCatalogEntry']
export type Interval = 'month' | 'year'

export interface Entitlements {
  max_members: number | null
  max_documents: number | null
  storage_bytes: number | null
  chat_tokens: number | null
  embedding_tokens: number | null
  audio_seconds: number | null
  figure_descriptions: number | null
  byok: boolean
  platform_ai: boolean
  server_voice: boolean
  custom_storage: boolean
  dedicated_database: boolean
  external_database: boolean
  customer_workers: boolean
}

export const ents = (plan: CatalogPlan) => plan.entitlements as unknown as Entitlements

export async function fetchPlans(signal?: AbortSignal): Promise<PlanCatalog> {
  const response = await fetch('/api/v1/plans', { signal, headers: { Accept: 'application/json' } })
  if (!response.ok) throw new Error(`plans: ${response.status}`)
  return (await response.json()) as PlanCatalog
}

/** The landing bundle stays free of the app's data layer: a plain fetch. */
export function usePlans(): { data: PlanCatalog | null; error: boolean } {
  const [state, setState] = useState<{ data: PlanCatalog | null; error: boolean }>({ data: null, error: false })
  useEffect(() => {
    const controller = new AbortController()
    fetchPlans(controller.signal)
      .then((data) => setState({ data, error: false }))
      .catch(() => {
        if (!controller.signal.aborted) setState({ data: null, error: true })
      })
    return () => controller.abort()
  }, [])
  return state
}

const USD = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
const USD_CENTS = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2 })

export function money(cents: number): string {
  return cents % 100 === 0 ? USD.format(cents / 100) : USD_CENTS.format(cents / 100)
}

/** Headline price: per member per month, for the chosen billing interval. */
export function priceLine(plan: CatalogPlan, interval: Interval): { amount: string; unit: string; note?: string } {
  if (plan.contact_sales || plan.monthly_cents_per_member === null) {
    return { amount: 'Custom', unit: 'tailored to your organization' }
  }
  if (plan.monthly_cents_per_member === 0) return { amount: '$0', unit: 'forever' }
  if (interval === 'year' && plan.annual_cents_per_member !== null) {
    return {
      amount: money(Math.round(plan.annual_cents_per_member / 12)),
      unit: 'per member / month',
      note: `${money(plan.annual_cents_per_member)} per member, billed yearly`,
    }
  }
  return { amount: money(plan.monthly_cents_per_member), unit: 'per member / month', note: 'Billed monthly' }
}

function count(n: number | null, noun: string): string {
  return n === null ? `Unlimited ${noun}` : `${n.toLocaleString('en-US')} ${noun}`
}

function bytes(n: number | null): string {
  if (n === null) return 'Unlimited storage'
  const gb = n / 1024 ** 3
  return `${gb >= 1000 ? `${gb / 1024} TB` : `${gb} GB`} storage`
}

function tokens(n: number | null): string {
  if (n === null) return 'Unlimited AI answers'
  return `${n >= 1_000_000 ? `${n / 1_000_000}M` : n.toLocaleString('en-US')} AI answer tokens a month`
}

/** Short, plain-language feature list for a plan card. */
export function highlights(plan: CatalogPlan): string[] {
  const e = ents(plan)
  const items = [
    e.max_members === null ? 'Unlimited members' : `Up to ${e.max_members} members`,
    count(e.max_documents, 'documents'),
    bytes(e.storage_bytes),
    tokens(e.chat_tokens),
    'Use your own AI key',
  ]
  if (e.custom_storage) items.push('Your own object storage (S3, MinIO, Azure)')
  if (e.dedicated_database) items.push('Dedicated database, hosted by us')
  if (e.external_database) items.push('Connect your own PostgreSQL database')
  if (e.customer_workers) items.push('Processing on your own servers')
  if (plan.key !== 'free') items.push('Two-factor sign-in for every member')
  return items
}

export interface ComparisonRow {
  label: string
  value: (plan: CatalogPlan) => string | boolean
}

export const COMPARISON: { group: string; rows: ComparisonRow[] }[] = [
  {
    group: 'Limits',
    rows: [
      { label: 'Members', value: (p) => (ents(p).max_members === null ? 'Unlimited' : String(ents(p).max_members)) },
      { label: 'Documents', value: (p) => count(ents(p).max_documents, '').trim() || 'Unlimited' },
      { label: 'Storage', value: (p) => bytes(ents(p).storage_bytes).replace(' storage', '') },
      { label: 'AI answer tokens a month', value: (p) => (ents(p).chat_tokens === null ? 'Unlimited' : `${ents(p).chat_tokens! / 1_000_000}M`) },
      {
        label: 'Speech a month',
        value: (p) => {
          const seconds = ents(p).audio_seconds
          if (seconds === null) return 'Unlimited'
          const hours = seconds / 3600
          return `${hours.toLocaleString('en-US')} ${hours === 1 ? 'hour' : 'hours'}`
        },
      },
      {
        label: 'Chart and figure descriptions a month',
        value: (p) => (ents(p).figure_descriptions === null ? 'Unlimited' : ents(p).figure_descriptions!.toLocaleString('en-US')),
      },
    ],
  },
  {
    group: 'AI',
    rows: [
      { label: "DocuMind's AI included", value: (p) => ents(p).platform_ai },
      { label: 'Use your own AI key (no limits on its usage)', value: (p) => ents(p).byok },
      { label: 'Read-aloud and voice questions', value: (p) => ents(p).server_voice },
    ],
  },
  {
    group: 'Your infrastructure',
    rows: [
      { label: 'Your own object storage (S3, MinIO, Azure Blob)', value: (p) => ents(p).custom_storage },
      { label: 'Dedicated database, hosted by DocuMind', value: (p) => ents(p).dedicated_database },
      { label: 'Your own PostgreSQL database', value: (p) => ents(p).external_database },
      { label: 'Processing on your own servers', value: (p) => ents(p).customer_workers },
    ],
  },
  {
    group: 'Security',
    rows: [
      { label: 'Tenant isolation, audit log, encrypted secrets', value: () => true },
      { label: 'Two-factor sign-in required for members', value: (p) => p.key !== 'free' },
      { label: 'Passkeys and one-time email codes', value: () => true },
    ],
  },
]

/** Where a plan's button goes from the public pages. */
export function planAction(plan: CatalogPlan, catalog: PlanCatalog, interval: Interval): { href: string; label: string } {
  if (plan.key === 'free') return { href: '/register', label: 'Get started free' }
  if (plan.contact_sales || !plan.self_serve) {
    return catalog.sales_email
      ? { href: `mailto:${catalog.sales_email}?subject=${encodeURIComponent(`DocuMind ${plan.name}`)}`, label: 'Contact sales' }
      : { href: '/register', label: 'Get started' }
  }
  const label = plan.trial_days > 0 ? `Start ${plan.trial_days}-day free trial` : `Choose ${plan.name}`
  return { href: `/app/billing?plan=${plan.key}&interval=${interval}`, label }
}
