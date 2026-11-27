/** Workspace settings as one tabbed place; extraction split into start and history. */
import { expect, test } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

test('settings pages share tabs and extraction has start and history tabs', async ({ page, baseURL }) => {
  test.setTimeout(120_000)
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Sam Settings', uniqueEmail('settings'))
  const root = page.url().replace(/\/dashboard.*$/, '')

  await page.goto(`${root}/settings`)
  const tabs = page.getByRole('navigation', { name: 'Workspace settings' })
  await expect(tabs.getByRole('link', { name: 'General' })).toHaveAttribute('aria-current', 'page')
  await expect(tabs.getByRole('link', { name: 'Members' })).toHaveCount(0) // personal workspace
  await tabs.getByRole('link', { name: 'Usage' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Usage' })).toBeVisible()
  await expect(tabs.getByRole('link', { name: 'Usage' })).toHaveAttribute('aria-current', 'page')
  await tabs.getByRole('link', { name: 'AI providers' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'AI providers' })).toBeVisible()

  // Not on other pages.
  await page.goto(`${root}/documents`)
  await expect(page.getByRole('heading', { level: 1, name: 'Documents' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Workspace settings' })).toHaveCount(0)

  // Organization: Members appears.
  await page.getByRole('button', { name: /Workspace: .*Switch workspace/ }).click()
  await page.getByRole('menuitem', { name: 'Create organization' }).click()
  await page.getByLabel('Organization name').fill('Tabs Org')
  await page.getByRole('button', { name: 'Create organization' }).last().click()
  await expect(page.getByRole('heading', { level: 1, name: 'Members' })).toBeVisible()
  const orgTabs = page.getByRole('navigation', { name: 'Organization settings' })
  await expect(orgTabs.getByRole('link', { name: 'Members' })).toHaveAttribute('aria-current', 'page')
  await page.screenshot({ path: 'test-results/settings-tabs.png' })

  const org = page.url().replace(/\/members.*$/, '')
  await page.goto(`${org}/extract`)
  const views = page.getByRole('navigation', { name: 'Extraction views' })
  await expect(views.getByRole('link', { name: 'Extract from documents' })).toHaveAttribute('aria-current', 'page')
  await views.getByRole('link', { name: /Extraction history/ }).click()
  await expect(page.getByText('No extractions yet')).toBeVisible()
})
