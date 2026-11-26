/**
 * Account self-service (ADR 0039): change email, download my data, delete an organization,
 * delete the account.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

import { linkFromEmail, local, PASSWORD, registerAndSignIn, totp, uniqueEmail } from './support.ts'

async function accessible(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
  expect(results.violations.map((v) => v.id)).toEqual([])
}

async function openProfile(page: Page) {
  await page.getByRole('button', { name: /Account menu/ }).click()
  await page.getByRole('menuitem', { name: 'Profile' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Profile' })).toBeVisible()
}

test('change email, export data, delete the account', async ({ page, baseURL }) => {
  test.setTimeout(180_000)
  const base = baseURL ?? 'http://localhost:5173'
  const email = uniqueEmail('self')
  const next = uniqueEmail('moved')
  await registerAndSignIn(page, base, 'Sam Self', email)
  await openProfile(page)

  // Email: a link goes to the new address; nothing changes until it's opened.
  const emailPanel = page.getByRole('region', { name: 'Email' })
  await emailPanel.getByLabel('New email').fill(next)
  await emailPanel.getByLabel('Current password').fill(PASSWORD)
  await emailPanel.getByRole('button', { name: 'Send confirmation link' }).click()
  await expect(emailPanel.getByText('Check the new address for a confirmation link.')).toBeVisible()
  await page.goto(local(await linkFromEmail(next, '/confirm-email-change'), base))
  await expect(page.getByRole('heading', { name: 'Email changed' })).toBeVisible()
  await page.getByRole('link', { name: 'Open DocuMind' }).click()
  await openProfile(page)
  await expect(page.getByRole('region', { name: 'Email' })).toContainText(next)

  // Your data: a ZIP prepared in the background.
  const data = page.getByRole('region', { name: 'Your data' })
  await data.getByRole('button', { name: 'Request a data export' }).click()
  await expect(data.getByRole('button', { name: 'Download' })).toBeVisible({ timeout: 60_000 })
  await expect(data.getByText('Ready')).toBeVisible()
  await accessible(page)

  // Delete the account.
  await page.getByRole('region', { name: 'Delete account' }).getByRole('button', { name: 'Delete my account' }).click()
  const dialog = page.getByRole('dialog', { name: 'Delete your account?' })
  await dialog.getByLabel('Current password').fill(PASSWORD)
  const confirmButton = dialog.getByRole('button', { name: 'Delete account' })
  await dialog.getByLabel('Type "DELETE" to confirm').fill('delete')
  await expect(confirmButton).toBeDisabled()
  await dialog.getByLabel('Type "DELETE" to confirm').fill('DELETE')
  await accessible(page)
  await confirmButton.click()
  await expect(page).toHaveURL(`${base}/`)
  const login = await page.request.post('/api/v1/auth/login', { data: { email: next, password: PASSWORD } })
  expect(login.status()).toBe(401)
})

test('owners delete an organization with their 2FA code', async ({ page, baseURL }) => {
  test.setTimeout(180_000)
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Olga Owner', uniqueEmail('orgdel'))

  await page.getByRole('button', { name: /Workspace: .*Switch workspace/ }).click()
  await page.getByRole('menuitem', { name: 'Create organization' }).click()
  await page.getByLabel('Organization name').fill('Doomed Org')
  await page.getByRole('button', { name: 'Create organization' }).last().click()
  await expect(page.getByRole('heading', { level: 1, name: 'Members' })).toBeVisible()

  // Deleting needs 2FA: set it up first.
  await page.getByRole('button', { name: /Account menu/ }).click()
  await page.getByRole('menuitem', { name: 'Security & 2FA' }).click()
  await page.getByRole('button', { name: 'Set up an authenticator app' }).click()
  await page.getByLabel('Confirm your password to continue').fill(PASSWORD)
  await page.getByRole('button', { name: 'Continue' }).click()
  const secret = (await page.getByLabel('Setup key').innerText()).replace(/\s/g, '')
  await page.getByLabel('Code from your app').fill(totp(secret))
  await page.getByRole('button', { name: 'Turn on 2FA' }).click()
  await page.getByRole('button', { name: "I've saved them" }).click()

  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Settings' }).click()
  const danger = page.getByRole('region', { name: 'Delete organization' })
  await danger.getByRole('button', { name: 'Delete Doomed Org' }).click()
  const dialog = page.getByRole('dialog', { name: 'Delete Doomed Org?' })
  await dialog.getByLabel('Type "Doomed Org" to confirm').fill('Doomed Org')
  await accessible(page)
  await dialog.getByRole('button', { name: 'Continue' }).click()
  // A fresh TOTP window: the setup code's window may still be current.
  await page.waitForTimeout(31_000 - (Date.now() % 30_000))
  await page.getByLabel('Authentication code').fill(totp(secret))
  await page.getByRole('dialog', { name: 'Delete Doomed Org' }).getByRole('button', { name: 'Delete organization' }).click()
  await expect(page.getByText('Doomed Org was deleted.')).toBeVisible()
  await expect(page).toHaveURL(/\/dashboard$/)
  await page.getByRole('button', { name: /Workspace: .*Switch workspace/ }).click()
  await expect(page.getByRole('menuitem', { name: /Doomed Org/ })).toHaveCount(0)
})
