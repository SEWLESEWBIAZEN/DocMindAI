/**
 * Extraction (ADR 0034): obligations with sources, contract terms compared side
 * by side, tracking a date as a deadline. The dev stack's offline model extracts.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

async function accessible(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
  expect(results.violations.map((v) => v.id)).toEqual([])
}

test('extract obligations, compare contracts and track a deadline', async ({ page, baseURL }) => {
  test.setTimeout(240_000)
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Ella Extract', uniqueEmail('extract'))
  await page.getByRole('region', { name: 'Getting started' }).getByRole('button', { name: 'Try sample documents' }).click()
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Documents' }).click()
  for (const name of [/Master Services Agreement/, /Employee Handbook/, /Aurora Thermostat FAQ/]) {
    await expect(page.getByRole('row', { name }).getByText('Ready')).toBeVisible({ timeout: 90_000 })
  }

  // Obligations from one document, started from the document's menu.
  await page.getByRole('button', { name: 'Actions for Sample: Master Services Agreement' }).click()
  await page.getByRole('menuitem', { name: 'Extract information' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Extract' })).toBeVisible()
  await expect(page.getByRole('checkbox', { name: 'Sample: Master Services Agreement' })).toBeChecked()
  await accessible(page)
  await page.getByRole('button', { name: 'Extract', exact: true }).click()
  await expect(page.getByText('Done', { exact: true })).toBeVisible({ timeout: 60_000 })
  const table = page.getByRole('table', { name: 'Extracted values' })
  await expect(table).toContainText('72 hours')
  await expect(table.getByRole('link', { name: /Source/ }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: 'Export CSV' })).toBeVisible()
  await accessible(page)

  // Contract terms across the sample collection, compared side by side.
  await page.getByRole('link', { name: 'Extract' }).first().click()
  await page.getByRole('radio', { name: /Contract key terms/ }).check()
  await page.getByRole('radio', { name: 'A whole collection' }).check()
  await page.getByRole('combobox', { name: 'Collection', exact: true }).selectOption({ label: 'Sample documents (3)' })
  await page.getByLabel('Name (optional)').fill('Sample contracts')
  await page.getByRole('button', { name: 'Extract', exact: true }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Sample contracts' })).toBeVisible()
  await expect(page.getByText('Done', { exact: true })).toBeVisible({ timeout: 60_000 })
  const compare = page.getByRole('table', { name: 'Fields compared across documents' })
  await expect(compare.getByRole('row', { name: /Governing law/ })).toContainText('England and Wales')
  await expect(compare.getByText('Differs').first()).toBeVisible()

  // Track the agreement's start date.
  await compare.getByRole('row', { name: /Effective date/ }).getByRole('button', { name: 'Track' }).first().click()
  const dialog = page.getByRole('dialog', { name: 'Track this deadline' })
  await expect(dialog.getByLabel('Due date')).toHaveValue('2026-03-01')
  await dialog.getByLabel('Title').fill('Agreement starts')
  await dialog.getByRole('button', { name: 'Add deadline' }).click()
  await expect(page.getByText('Deadline added.')).toBeVisible()

  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Deadlines' }).click()
  const overdue = page.getByRole('region', { name: 'Overdue' })
  await expect(overdue).toContainText('Agreement starts')
  await expect(overdue.getByRole('link', { name: 'Sample: Master Services Agreement' })).toBeVisible()
  await accessible(page)
  await overdue.getByRole('button', { name: 'Done', exact: true }).click()
  await expect(page.getByText('Marked as done.')).toBeVisible()
  await expect(page.getByText('No upcoming deadlines')).toBeVisible()
})
