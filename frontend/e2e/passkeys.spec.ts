/**
 * Passkeys (ADR 0029) with Chromium's virtual authenticator, and the
 * support-assisted reset request (ADR 0030) from the sign-in screen.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

import { PASSWORD, registerAndSignIn, uniqueEmail } from './support.ts'

async function virtualAuthenticator(page: Page): Promise<void> {
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('WebAuthn.enable')
  await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2',
      transport: 'internal',
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  })
}

async function signInUntilChallenge(page: Page, email: string): Promise<void> {
  await page.getByLabel('Email').fill(email)
  await page.getByLabel('Password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Two-factor authentication' })).toBeVisible()
}

test('passkeys: add one, then sign in with it; ask support for a reset', async ({ page, baseURL }) => {
  await virtualAuthenticator(page)
  const email = uniqueEmail('passkey')
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Pat Passkey', email)

  await page.getByRole('button', { name: /Account menu/ }).click()
  await page.getByRole('menuitem', { name: 'Security & 2FA' }).click()
  await page.getByRole('button', { name: 'Add a passkey' }).click()
  await page.getByLabel('Passkey name').fill('Test device')
  await page.getByLabel('Your password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Create passkey' }).click()
  // The first method turns 2FA on, with recovery codes.
  await expect(page.getByRole('list', { name: 'Passkey recovery codes' })).toBeVisible()
  await page.getByRole('button', { name: "I've saved them" }).click()
  await expect(page.getByRole('list', { name: 'Your passkeys' })).toContainText('Test device')
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
  expect(results.violations.map((v) => v.id)).toEqual([])

  await page.getByRole('button', { name: /Account menu/ }).click()
  await page.getByRole('menuitem', { name: 'Sign out' }).click()
  await page.waitForURL('**/login')
  await signInUntilChallenge(page, email)
  await page.getByRole('button', { name: 'Sign in with a passkey' }).click()
  await page.waitForURL(/\/w\/[^/]+\/(dashboard|profile)/)

  // Someone who lost everything asks support for a reset (here: the same account).
  await page.getByRole('button', { name: /Account menu/ }).click()
  await page.getByRole('menuitem', { name: 'Sign out' }).click()
  await page.waitForURL('**/login')
  await signInUntilChallenge(page, email)
  await page.getByRole('button', { name: 'Lost access to all of these? Ask support to reset 2FA' }).click()
  await page.getByLabel('What happened?').fill('My laptop was stolen and I no longer have access to that email.')
  await page.getByRole('button', { name: 'Send reset request' }).click()
  await expect(page.getByText('Support will verify your identity before anything changes')).toBeVisible()
})
