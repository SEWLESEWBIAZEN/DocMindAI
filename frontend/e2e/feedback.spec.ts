/**
 * Answer feedback (ADR 0038): rate an answer, change the rating with reasons, and see it
 * counted on the admin "Answer quality" page.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

async function accessible(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
  expect(results.violations.map((v) => v.id)).toEqual([])
}

test('rate answers and see the summary', async ({ page, baseURL }) => {
  test.setTimeout(180_000)
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Fay Feedback', uniqueEmail('feedback'))
  await page.getByRole('region', { name: 'Getting started' }).getByRole('button', { name: 'Try sample documents' }).click()
  await expect(page.getByText('Sample documents added.')).toBeVisible()
  const nav = page.getByRole('navigation', { name: 'Main' })
  await nav.getByRole('link', { name: 'Documents' }).click()
  for (const name of [/Master Services Agreement/, /Employee Handbook/, /Aurora Thermostat FAQ/]) {
    await expect(page.getByRole('row', { name }).getByText('Ready')).toBeVisible({ timeout: 90_000 })
  }

  await nav.getByRole('link', { name: 'Ask' }).click()
  await page.reload() // the "no documents yet" check was cached before processing finished
  await page.getByRole('button', { name: 'How much notice is needed for non-renewal of the services agreement?' }).click()
  const answer = page.getByRole('article', { name: 'Answer' }).last()
  const good = answer.getByRole('button', { name: 'Good answer' })
  await expect(good).toBeVisible({ timeout: 60_000 })

  await good.click()
  await expect(good).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByText('Thanks for the feedback.')).toBeVisible()

  // Change it to a thumbs down with reasons. A personal workspace has no admins to share with.
  await answer.getByRole('button', { name: 'Bad answer' }).click()
  const dialog = page.getByRole('dialog', { name: 'What was wrong with this answer?' })
  await dialog.getByLabel('Incomplete').check()
  await dialog.getByLabel("Sources don't support it").check()
  await dialog.getByLabel('Anything else? (optional)').fill('It skipped the auto-renewal clause.')
  await expect(dialog.getByText('Share this question and answer')).toHaveCount(0)
  await accessible(page)
  await dialog.getByRole('button', { name: 'Send feedback' }).click()
  await expect(dialog).toBeHidden()
  await expect(answer.getByRole('button', { name: 'Bad answer' })).toHaveAttribute('aria-pressed', 'true')
  await expect(good).toHaveAttribute('aria-pressed', 'false')

  // The rating is still there after a reload.
  await page.reload()
  await expect(page.getByRole('article', { name: 'Answer' }).last().getByRole('button', { name: 'Bad answer' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )

  await nav.getByRole('link', { name: 'Answer quality' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Answer quality' })).toBeVisible()
  const helpful = page.locator('.stat', { hasText: 'Rated helpful' })
  await expect(helpful).toContainText('0%')
  await expect(helpful).toContainText('0 up · 1 down')
  const reasons = page.getByRole('region', { name: 'Why answers were rated down' })
  await expect(reasons.getByRole('listitem').filter({ hasText: 'Incomplete' })).toContainText('1')
  await accessible(page)
})
