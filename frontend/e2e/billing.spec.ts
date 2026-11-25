/**
 * Pricing page and in-app billing (ADR 0032). The development stack runs without
 * Stripe keys, so self-serve checkout is off there; the checkout redirect is
 * exercised by intercepting the overview and checkout calls.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

const TAGS = ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']

async function accessible(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze()
  expect(results.violations.map((v) => v.id)).toEqual([])
}

test('public pricing page lists tiers, prices and custom infrastructure', async ({ page }) => {
  // Cards fade in; measure contrast on the settled page.
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/pricing')
  await expect(page.getByRole('heading', { level: 1, name: 'Simple plans that grow with your team' })).toBeVisible()
  const pro = page.getByRole('listitem').filter({ has: page.getByRole('heading', { name: 'Pro', exact: true }) })
  await expect(pro.getByText('$15.83')).toBeVisible()
  await page.getByText('Monthly', { exact: true }).click()
  await expect(pro.getByText('$19', { exact: true })).toBeVisible()
  await expect(pro.getByText('Your own object storage (S3, MinIO, Azure)')).toBeVisible()
  const business = page.getByRole('listitem').filter({ has: page.getByRole('heading', { name: 'Business', exact: true }) })
  await expect(business.getByText('$99', { exact: true })).toBeVisible()
  await expect(business.getByText('Connect your own PostgreSQL database')).toBeVisible()
  await expect(page.getByRole('table', { name: 'Compare plans' })).toContainText('Processing on your own servers')
  await accessible(page)
  await page.setViewportSize({ width: 390, height: 844 })
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(0)
})

test('owners see plans in the app and are sent to Stripe Checkout', async ({ page, baseURL }) => {
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Bea Billing', uniqueEmail('billing'))
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Billing' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Billing' })).toBeVisible()
  await expect(page.getByRole('heading', { name: /Free plan/ })).toBeVisible()
  await expect(page.getByText("Online payments aren't set up on this server.")).toBeVisible()
  await accessible(page)

  // As on a server with Stripe configured:
  await page.route(/\/api\/v1\/workspaces\/[^/]+\/billing$/, async (route) => {
    const response = await route.fetch()
    await route.fulfill({ response, json: { ...(await response.json()), enabled: true } })
  })
  await page.route(/\/api\/v1\/plans$/, async (route) => {
    const response = await route.fetch()
    const body = await response.json()
    for (const plan of body.plans) plan.self_serve = ['pro', 'business'].includes(plan.key)
    await route.fulfill({ response, json: { ...body, billing_enabled: true } })
  })
  let requested: unknown = null
  await page.route(/\/api\/v1\/workspaces\/[^/]+\/billing\/checkout$/, async (route) => {
    requested = route.request().postDataJSON()
    await route.fulfill({ status: 201, json: { url: `${baseURL ?? 'http://localhost:5173'}/pricing?stripe=1` } })
  })
  await page.reload()
  await page.getByRole('radio', { name: 'Monthly' }).check()
  await page.getByRole('button', { name: 'Upgrade to Business' }).click()
  await page.waitForURL('**/pricing?stripe=1')
  expect(requested).toEqual({ plan: 'business', interval: 'month' })
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})
