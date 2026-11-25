import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Pricing } from '../sections/Pricing'
import { highlights, planAction, priceLine, type CatalogPlan, type Entitlements, type PlanCatalog } from './catalog'

const base: Entitlements = {
  max_members: 5,
  max_documents: 200,
  storage_bytes: 1024 ** 3,
  chat_tokens: 2_000_000,
  embedding_tokens: 20_000_000,
  audio_seconds: 3600,
  figure_descriptions: 200,
  byok: true,
  platform_ai: true,
  server_voice: true,
  custom_storage: false,
  dedicated_database: false,
  external_database: false,
  customer_workers: false,
}

function plan(key: string, name: string, monthly: number | null, extra: Partial<Entitlements> = {}): CatalogPlan {
  return {
    key,
    name,
    description: `${name} plan`,
    monthly_cents_per_member: monthly,
    annual_cents_per_member: monthly === null ? null : monthly * 10,
    self_serve: monthly !== null && monthly > 0,
    contact_sales: monthly === null,
    entitlements: { ...base, ...extra } as unknown as CatalogPlan['entitlements'],
  }
}

const CATALOG: PlanCatalog = {
  currency: 'usd',
  annual_months_charged: 10,
  billing_enabled: true,
  sales_email: 'sales@example.com',
  infrastructure_features: {},
  plans: [
    plan('free', 'Free', 0),
    plan('pro', 'Pro', 1900, { max_members: 25, custom_storage: true }),
    plan('business', 'Business', 9900, {
      max_members: 250,
      custom_storage: true,
      dedicated_database: true,
      external_database: true,
    }),
    plan('enterprise', 'Enterprise', null, {
      max_members: null,
      max_documents: null,
      storage_bytes: null,
      chat_tokens: null,
      custom_storage: true,
      dedicated_database: true,
      external_database: true,
      customer_workers: true,
    }),
  ],
}

afterEach(() => vi.unstubAllGlobals())

describe('pricing helpers', () => {
  it('shows per-member prices, with yearly as a monthly equivalent', () => {
    const [free, pro, business, enterprise] = CATALOG.plans
    expect(priceLine(free, 'month')).toEqual({ amount: '$0', unit: 'forever' })
    expect(priceLine(pro, 'month')).toMatchObject({ amount: '$19', unit: 'per member / month' })
    expect(priceLine(pro, 'year')).toMatchObject({ amount: '$15.83', note: '$190 per member, billed yearly' })
    expect(priceLine(business, 'year').note).toBe('$990 per member, billed yearly')
    expect(priceLine(enterprise, 'year').amount).toBe('Custom')
  })

  it('lists custom infrastructure on the plans that include it', () => {
    const [free, pro, business, enterprise] = CATALOG.plans.map(highlights)
    expect(free.join()).not.toContain('object storage')
    expect(pro).toContain('Your own object storage (S3, MinIO, Azure)')
    expect(pro.join()).not.toContain('database')
    expect(business).toEqual(
      expect.arrayContaining(['Dedicated database, hosted by us', 'Connect your own PostgreSQL database']),
    )
    expect(enterprise).toContain('Processing on your own servers')
    expect(enterprise[0]).toBe('Unlimited members')
  })

  it('sends paid plans to billing and Enterprise to sales', () => {
    const [free, pro, , enterprise] = CATALOG.plans
    expect(planAction(free, CATALOG, 'year').href).toBe('/register')
    expect(planAction(pro, CATALOG, 'year')).toEqual({ href: '/app/billing?plan=pro&interval=year', label: 'Choose Pro' })
    expect(planAction(enterprise, CATALOG, 'month').href).toMatch(/^mailto:sales@example\.com/)
    const offline = { ...CATALOG, sales_email: null, plans: CATALOG.plans.map((p) => ({ ...p, self_serve: false })) }
    expect(planAction(offline.plans[1], offline, 'month').href).toBe('/register')
  })
})

describe('Pricing section', () => {
  it('loads the catalog and switches between yearly and monthly', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(CATALOG), { status: 200 })))
    render(<Pricing full />)
    expect(await screen.findByRole('heading', { level: 3, name: 'Business' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'Simple plans that grow with your team' })).toBeInTheDocument()
    const pro = screen.getByRole('heading', { level: 3, name: 'Pro' }).closest('.lp-plan') as HTMLElement
    expect(within(pro).getByText('$15.83')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('radio', { name: 'Monthly' }))
    expect(within(pro).getByText('$19')).toBeInTheDocument()
    expect(within(pro).getByRole('link', { name: 'Choose Pro' })).toHaveAttribute('href', '/app/billing?plan=pro&interval=month')
    const table = screen.getByRole('table', { name: 'Compare plans' })
    const row = within(table).getByRole('row', { name: /Your own PostgreSQL database/ })
    expect(within(row).getAllByText('Included')).toHaveLength(2)
    expect(screen.getByText('Do you store card details?')).toBeInTheDocument()
  })

  it('degrades to a sign-up link when prices cannot be loaded', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('nope', { status: 503 })))
    render(<Pricing />)
    expect(await screen.findByText(/Prices couldn't be loaded/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Start free' })).toHaveAttribute('href', '/register')
  })
})
