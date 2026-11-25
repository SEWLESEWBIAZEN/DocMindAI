/**
 * Documents indexed with an older embedding model are flagged, and owners can
 * re-index them (ADR 0031). Changing the model needs server configuration, so the
 * list response is marked as stale here; the backend side is covered by
 * test_documents_on_another_model_are_reported_not_silently_skipped.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

test('stale documents show a badge and a "Re-index all" banner', async ({ page, baseURL }) => {
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Rita Reindex', uniqueEmail('reindex'))
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Upload' }).click()
  await page.locator('input[type=file]').last().setInputFiles({
    name: 'old-notes.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from('# Old notes\n\nThe renewal deadline is the first of March.\n'),
  })
  await page.getByRole('link', { name: 'View all documents' }).click()
  const row = page.getByRole('row', { name: /old-notes/ })
  await expect(row.getByText('Ready')).toBeVisible({ timeout: 90_000 })

  await page.route(/\/api\/v1\/workspaces\/[^/]+\/documents(\?.*)?$/, async (route) => {
    if (route.request().method() !== 'GET') return route.continue()
    const response = await route.fetch()
    const body = await response.json()
    body.needs_reindex_total = body.items.length
    for (const item of body.items) item.needs_reindex = true
    await route.fulfill({ response, json: body })
  })
  await page.reload()
  await expect(row.getByText('Needs re-indexing')).toBeVisible()
  const banner = page.locator('#reindex')
  await expect(banner).toContainText('1 document was indexed with a different AI model.')
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
  expect(results.violations.map((v) => v.id)).toEqual([])

  await banner.getByRole('button', { name: 'Re-index all' }).click()
  // Nothing is really stale on the server, so nothing is queued; the action is wired up.
  await expect(page.getByText(/^Re-indexing \d+ documents?\.$/)).toBeVisible()
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})
