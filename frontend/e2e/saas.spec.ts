import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

async function expectAccessible(page: Page, label: string) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze()
  const serious = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical')
  expect(
    serious.map((v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(' ')).join(', ')})`),
    `accessibility violations on ${label}`,
  ).toEqual([])
}

test('plan, AI providers and infrastructure settings (Phase 8)', async ({ page, baseURL }) => {
  const base = baseURL ?? 'http://localhost:5173'
  await registerAndSignIn(page, base, 'Petra Planner', uniqueEmail('planner'))

  // Usage: plan and quotas.
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Usage' }).click()
  await expect(page.getByRole('heading', { name: 'Free plan' })).toBeVisible()
  await expect(page.getByText('AI answer tokens')).toBeVisible()
  await expect(page.getByRole('meter').first()).toBeVisible()
  await expectAccessible(page, 'usage with plan')

  // AI providers: platform key in use; a key is never shown back.
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'AI providers' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'AI providers' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Which key each feature uses' })).toBeVisible()
  await expect(page.getByText("DocuMind's key").first()).toBeVisible()
  await expect(page.getByText('Anthropic')).toBeVisible() // planned providers listed
  await expectAccessible(page, 'ai providers')
  // Saving a key asks for 2FA first (this user has none yet).
  await page.getByLabel('API key').fill('sk-proj-this-is-not-a-real-key-000000')
  await page.getByRole('button', { name: 'Save key' }).click()
  await expect(page.getByRole('dialog', { name: 'Two-factor authentication required' })).toBeVisible()
  await page.getByRole('button', { name: 'Cancel' }).click()

  // Infrastructure: organizations only, owners only.
  await page.getByRole('button', { name: /Workspace: .*Switch workspace/ }).click()
  await page.getByRole('menuitem', { name: 'Create organization' }).click()
  await page.getByLabel('Organization name').fill('Infra Org')
  await page.getByRole('button', { name: 'Create organization' }).last().click()
  await expect(page.getByRole('heading', { level: 1, name: 'Members' })).toBeVisible()
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Infrastructure' }).click()
  await expect(page.getByRole('heading', { name: 'Current infrastructure' })).toBeVisible()
  await expect(page.getByText('DocuMind defaults')).toBeVisible()
  await expect(page.getByRole('radio', { name: /Your own PostgreSQL/ })).toBeDisabled() // not in the Free plan
  await expect(page.getByRole('option', { name: /Azure Blob Storage \(planned\)/ })).toBeDisabled()
  await expectAccessible(page, 'infrastructure')
})

test('Phase 8 pages fit a phone screen', async ({ browser, baseURL }) => {
  const base = baseURL ?? 'http://localhost:5173'
  const context = await browser.newContext({ baseURL: base, viewport: { width: 390, height: 844 }, deviceScaleFactor: 1 })
  const page = await context.newPage()
  await registerAndSignIn(page, base, 'Mo Bile', uniqueEmail('mobile8'))
  const root = page.url().replace(/\/(dashboard)?$/, '').replace(/\/dashboard.*$/, '')
  for (const path of ['usage', 'ai-providers']) {
    await page.goto(`${root}/${path}`)
    await page.getByRole('heading', { level: 1 }).first().waitFor()
    const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(wide, `horizontal overflow on ${path}`).toBeLessThanOrEqual(0)
  }
  await context.close()
})
