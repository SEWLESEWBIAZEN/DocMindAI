/** Chat: the cited page opens beside the answer, with the passage highlighted. */
import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

test('a citation opens the source preview beside the answer', async ({ page, baseURL }) => {
  test.setTimeout(180_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Cy Cite', uniqueEmail('cite'))
  await page.getByRole('region', { name: 'Getting started' }).getByRole('button', { name: 'Try sample documents' }).click()
  await expect(page.getByText('Sample documents added.')).toBeVisible()
  const nav = page.getByRole('navigation', { name: 'Main' })
  await nav.getByRole('link', { name: 'Documents' }).click()
  await expect(page.getByRole('row', { name: /Master Services Agreement/ }).getByText('Ready')).toBeVisible({ timeout: 90_000 })

  await nav.getByRole('link', { name: 'Ask' }).click()
  await page.getByRole('button', { name: 'How much notice is needed for non-renewal of the services agreement?' }).click()
  const answer = page.getByRole('article', { name: 'Answer' }).last()
  await expect(answer.getByRole('button', { name: 'Source 1' }).first()).toBeVisible({ timeout: 60_000 })

  await answer.getByRole('button', { name: 'Source 1' }).first().click()
  const preview = page.getByRole('region', { name: 'Source preview' })
  await expect(preview).toBeVisible()
  await expect(preview.getByText(/Master Services Agreement/).first()).toBeVisible()
  await expect(preview.locator('mark.cite-highlight')).toBeVisible()
  await expect(preview.getByRole('link', { name: /Open document/ })).toBeVisible()

  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme })
    await page.waitForTimeout(600)
    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
    expect(results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical').map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([])
  }
  await page.emulateMedia({ colorScheme: 'light' })
  await page.screenshot({ path: 'test-results/chat-preview-layout.png' })

  await preview.getByRole('button', { name: 'Close source preview' }).click()
  await expect(preview).toBeHidden()
  // The source card's Preview button opens it too.
  await answer.getByRole('button', { name: /^Preview / }).first().click()
  await expect(page.getByRole('region', { name: 'Source preview' })).toBeVisible()
})
