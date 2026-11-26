/**
 * Per-collection access and single sign-on screens (ADR 0036). Hiding documents from
 * members is covered end to end by the API tests; here: the admin UI and sign-in.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

async function accessible(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
  expect(results.violations.map((v) => v.id)).toEqual([])
}

test('admins restrict a collection; organizations see SSO settings', async ({ page, baseURL }) => {
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Rae Restrict', uniqueEmail('access'))
  await page.goto('/app')
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Organizations' }).click()
  await page.getByRole('button', { name: 'Create organization' }).click()
  await page.getByLabel('Name').fill('Acme Access')
  await page.getByRole('button', { name: 'Create organization' }).click()
  await page.waitForURL(/\/w\/[^/]+\/members/)
  const base = new URL(page.url()).pathname.replace(/\/members$/, '')
  await page.goto(`${base}/collections`)
  await page.getByRole('button', { name: 'New collection' }).first().click()
  const dialog = page.getByRole('dialog', { name: 'New collection' })
  await dialog.getByLabel('Name').fill('HR')
  await dialog.getByRole('button', { name: /Create/ }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'HR' })).toBeVisible()
  const panel = page.getByRole('region', { name: /Who can see it/ })
  await panel.getByRole('radio', { name: 'Only chosen members' }).check()
  await expect(panel.getByText('There are no members or viewers to choose yet.')).toBeVisible()
  await panel.getByRole('button', { name: 'Save access' }).click()
  await expect(page.getByText('Only the chosen members (and admins) can see this collection now.')).toBeVisible()
  await expect(panel.getByText('Restricted')).toBeVisible()
  await accessible(page)

  await page.goto(`${base}/settings`)
  const sso = page.getByRole('region', { name: /Single sign-on/ })
  await expect(sso.getByText('Included from the Business plan.', { exact: false })).toBeVisible()
  await expect(sso.getByLabel('Issuer URL')).toBeVisible()
  await accessible(page)
})

test('sign-in offers single sign-on and explains failures', async ({ page }) => {
  await page.goto('/login?sso_error=browser')
  await expect(page.getByText('Finish signing in with the same browser you started in.')).toBeVisible()
  await page.getByRole('button', { name: 'Sign in with single sign-on (SSO)' }).click()
  await page.getByLabel('Work email').fill('someone@unknown-sso.example.com')
  await page.getByRole('button', { name: 'Continue with single sign-on' }).click()
  await expect(page.getByText("Single sign-on isn't set up for that email address.")).toBeVisible()
  await accessible(page)
})
