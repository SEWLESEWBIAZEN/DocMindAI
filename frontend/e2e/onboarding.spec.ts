/**
 * First run (ADR 0033): the getting-started checklist, sample documents, a real
 * cited answer from them, and removing them again.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

test('try DocuMind with sample documents, then remove them', async ({ page, baseURL }) => {
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Nia Newcomer', uniqueEmail('samples'))
  const checklist = page.getByRole('region', { name: 'Getting started' })
  await expect(checklist).toContainText('0 of 3 done')
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
  expect(results.violations.map((v) => v.id)).toEqual([])

  await checklist.getByRole('button', { name: 'Try sample documents' }).click()
  await expect(page.getByText('Sample documents added.')).toBeVisible()
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Documents' }).click()
  const agreement = page.getByRole('row', { name: /Sample: Master Services Agreement/ })
  await expect(agreement.getByText('Sample', { exact: true })).toBeVisible()
  for (const name of [/Master Services Agreement/, /Employee Handbook/, /Aurora Thermostat FAQ/]) {
    await expect(page.getByRole('row', { name }).getByText('Ready')).toBeVisible({ timeout: 90_000 })
  }

  // The chat suggests questions the samples can answer.
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Ask' }).click()
  await page.getByRole('button', { name: 'How much notice is needed for non-renewal of the services agreement?' }).click()
  await expect(page.getByText(/90 days/).first()).toBeVisible({ timeout: 60_000 })
  await expect(page.locator('.citations').first()).toContainText('Sample: Master Services Agreement')

  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Dashboard' }).click()
  await expect(checklist).toContainText('2 of 3 done')
  await checklist.getByRole('button', { name: 'Remove them' }).click()
  await expect(page.getByText('Removed 3 sample documents.')).toBeVisible()
  // The question asked still counts; the documents step is open again.
  await expect(checklist).toContainText('1 of 3 done')
})
