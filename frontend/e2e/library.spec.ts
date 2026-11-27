/** Document library: tabs, type and collection filters, list and grid layouts. */
import { expect, test } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

test('filter the library by tab, type and collection, in list or grid', async ({ page, baseURL }) => {
  test.setTimeout(180_000)
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Lia Library', uniqueEmail('library'))
  await page.getByRole('region', { name: 'Getting started' }).getByRole('button', { name: 'Try sample documents' }).click()
  await expect(page.getByText('Sample documents added.')).toBeVisible()
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Documents' }).click()
  for (const name of [/Master Services Agreement/, /Employee Handbook/, /Aurora Thermostat FAQ/]) {
    await expect(page.getByRole('row', { name }).getByText('Ready')).toBeVisible({ timeout: 90_000 })
  }

  const tabs = page.getByRole('navigation', { name: 'Document views' })
  await expect(tabs.getByRole('link', { name: 'All' })).toHaveAttribute('aria-current', 'page')
  await tabs.getByRole('link', { name: 'Failed' }).click()
  await expect(page.getByText('No documents match')).toBeVisible()
  await tabs.getByRole('link', { name: 'Ready' }).click()
  await expect(page.getByRole('row')).toHaveCount(4) // header + three samples

  await page.getByLabel('Type').selectOption('pdf')
  await expect(page.getByText('No documents match')).toBeVisible()
  await page.getByLabel('Type').selectOption('')
  await page.getByLabel('Collection').selectOption({ label: 'Sample documents' })
  await expect(page.getByRole('row')).toHaveCount(4)

  const search = page.getByLabel('Search documents by title or filename')
  await search.fill('handbook')
  await search.press('Enter')
  await expect(page.getByRole('row')).toHaveCount(2)

  await page.getByRole('button', { name: 'Grid view' }).click()
  const grid = page.getByRole('list', { name: /Documents, 1 in total/ })
  await expect(grid.getByRole('link', { name: /Employee Handbook/ })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('button', { name: 'Grid view' })).toHaveAttribute('aria-pressed', 'true')
  await page.screenshot({ path: 'test-results/library-layout.png' })
  await page.getByRole('button', { name: 'List view' }).click()
  await expect(page.getByRole('table')).toBeVisible()
})
