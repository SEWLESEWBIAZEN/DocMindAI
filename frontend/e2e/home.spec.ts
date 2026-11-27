/** Search-first home: ask from the dashboard, quick actions, recent documents and work. */
import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

test('ask from the home page and pick up where you left off', async ({ page, baseURL }) => {
  test.setTimeout(180_000)
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Hana Home', uniqueEmail('home'))
  await expect(page.getByRole('heading', { level: 1, name: /^Good (morning|afternoon|evening), Hana$/ })).toBeVisible()
  const actions = page.getByRole('list', { name: 'Quick actions' })
  for (const name of ['Summarize a document', 'Compare documents', 'Extract information', 'Find deadlines']) {
    await expect(actions.getByRole('link', { name })).toBeVisible()
  }

  await page.getByRole('region', { name: 'Getting started' }).getByRole('button', { name: 'Try sample documents' }).click()
  await expect(page.getByText('Sample documents added.')).toBeVisible()
  const recent = page.getByRole('region', { name: 'Recent documents' })
  await expect(recent.getByText('Ready').first()).toBeVisible({ timeout: 90_000 })
  await expect(recent.getByRole('link', { name: /Master Services Agreement/ })).toBeVisible()

  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme })
    await page.waitForTimeout(600)
    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
    expect(results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical').map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([])
  }
  await page.emulateMedia({ colorScheme: 'light' })
  await page.screenshot({ path: 'test-results/home-layout.png' })

  // The question goes straight to a new conversation.
  const ask = page.getByRole('search', { name: 'Ask your documents' })
  await ask.getByLabel('Ask anything about your documents').fill('How much notice is needed for non-renewal of the services agreement?')
  await ask.getByRole('button', { name: 'Ask' }).click()
  await page.waitForURL(/\/chat\/[0-9a-f-]{36}$/)
  await expect(page.getByRole('article', { name: 'Answer' }).last()).toBeVisible({ timeout: 60_000 })

  // Back home, the conversation is under "Continue your work".
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Dashboard' }).click()
  await expect(page.getByRole('region', { name: 'Continue your work' }).getByText(/You asked/)).toBeVisible()

  await page.setViewportSize({ width: 390, height: 844 })
  await page.reload()
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})
