/**
 * Email-only two-factor authentication (ADR 0028): for people without an
 * authenticator app. Set up with a code from the inbox, then sign in with a
 * code that is sent automatically.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

import { PASSWORD, codeFromEmail, registerAndSignIn, uniqueEmail } from './support.ts'

test('two-factor authentication with codes by email', async ({ page, baseURL }) => {
  const email = uniqueEmail('mail2fa')
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Esther Email', email)

  await page.getByRole('button', { name: /Account menu/ }).click()
  await page.getByRole('menuitem', { name: 'Security & 2FA' }).click()
  await page.getByRole('button', { name: 'Use codes by email instead' }).click()
  await page.getByLabel('Confirm your password to continue').fill(PASSWORD)
  await page.getByRole('button', { name: 'Email me a code' }).click()
  await page.getByLabel('Email code').fill(await codeFromEmail(email))
  await page.getByRole('button', { name: 'Turn on two-factor authentication' }).click()
  await expect(page.getByRole('list', { name: 'Recovery codes' })).toBeVisible()
  await page.getByRole('button', { name: "I've saved them" }).click()
  await expect(page.getByText('On · email codes')).toBeVisible()
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
  expect(results.violations.map((v) => v.id)).toEqual([])

  // Sign out, then in: the code is emailed without asking.
  await page.getByRole('button', { name: /Account menu/ }).click()
  await page.getByRole('menuitem', { name: 'Sign out' }).click()
  await page.waitForURL('**/login')
  await page.getByLabel('Email').fill(email)
  await page.getByLabel('Password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByText('We sent a one-time code to your email address')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Use your authenticator app' })).toHaveCount(0)
  await page.getByLabel('Email code').fill(await codeFromEmail(email))
  await page.getByRole('button', { name: 'Verify and sign in' }).click()
  await page.waitForURL(/\/w\/.+\/dashboard/)
})
