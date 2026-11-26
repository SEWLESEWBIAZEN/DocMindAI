/** Connectors page (ADR 0035). The dev stack has no Google OAuth client configured. */
import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

test('admins see how to set up Google Drive', async ({ page, baseURL }) => {
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Cora Connect', uniqueEmail('connect'))
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Connectors' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Connectors' })).toBeVisible()
  await expect(page.getByText("Google Drive isn't set up on this server yet.")).toBeVisible()
  await expect(page.getByText('/api/v1/connectors/google-drive/callback')).toBeVisible()
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
  expect(results.violations.map((v) => v.id)).toEqual([])
  // A failed connection attempt comes back with a readable reason.
  await page.goto(page.url() + '?connector_error=browser')
  await expect(page.getByText('Finish connecting in the same browser you started in.')).toBeVisible()
})
