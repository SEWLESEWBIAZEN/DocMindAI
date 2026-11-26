/**
 * "Continue with Google" (ADR 0040): what the app does after Google. Google itself can't be
 * reached from the test stack, where the feature is off, so the button is hidden; the
 * callback's hand-offs to the sign-in page are exercised directly.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

test('the sign-in page continues where Google left off', async ({ page }) => {
  // Off on this server: no button.
  await page.goto('/login')
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Continue with Google' })).toHaveCount(0)
  await page.goto('/register')
  await expect(page.getByRole('button', { name: 'Continue with Google' })).toHaveCount(0)

  // Errors from the callback are explained.
  await page.goto('/login?google_error=denied')
  await expect(page.getByText('Signing in with Google was cancelled.')).toBeVisible()

  // A work address whose organization uses SSO: straight to the SSO form, prefilled.
  await page.goto('/login#sso=alice%40acme.example.com')
  await expect(page.getByText('Your organization signs you in with single sign-on.')).toBeVisible()
  await expect(page.getByLabel('Work email')).toHaveValue('alice@acme.example.com')
  await expect(page.getByRole('button', { name: 'Continue with single sign-on' })).toBeVisible()
  expect(new URL(page.url()).hash).toBe('')

  // People with 2FA land on the code step; the challenge never stays in the address bar.
  // (Coming back from Google is a full page load; a hash-only change on /login is not.)
  await page.goto('about:blank')
  await page.goto('/login#mfa=not-a-real-challenge&methods=authenticator,email,recovery_code')
  await expect(page.getByLabel('Authentication code')).toBeVisible()
  expect(new URL(page.url()).hash).toBe('')
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
  expect(results.violations.map((v) => v.id)).toEqual([])
  await page.getByLabel('Authentication code').fill('123456')
  await page.getByRole('button', { name: 'Verify and sign in' }).click()
  await expect(page.getByText(/expired/i).first()).toBeVisible()
})
