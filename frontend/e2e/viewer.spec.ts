/**
 * Document viewer: the document in the centre, questions about it answered in the docked
 * "Document intelligence" panel, and citations that scroll to and highlight the passage.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

const POLICY = `# Vendor Policy

## Payment

Invoices are payable within thirty days of receipt.

## Termination

Either party may terminate the agreement with ninety days written notice.
`

test('ask about a document beside it and jump to the cited passage', async ({ page, baseURL }) => {
  test.setTimeout(180_000)
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Vic Viewer', uniqueEmail('viewer'))
  const root = page.url().replace(/\/dashboard.*$/, '')
  await page.goto(`${root}/upload`)
  await page.locator('input[type=file]').last().setInputFiles({ name: 'vendor-policy.md', mimeType: 'text/markdown', buffer: Buffer.from(POLICY) })
  await expect(page.getByText('Uploaded. Processing has started.')).toBeVisible()
  await page.goto(`${root}/documents`)
  const row = page.getByRole('row', { name: /vendor-policy/ })
  await expect(row.getByText('Ready')).toBeVisible({ timeout: 90_000 })
  await row.getByRole('link').first().click()

  // Layout: breadcrumb, reading pane with its toolbar, and the docked panel.
  await expect(page.getByRole('navigation', { name: 'Breadcrumb' }).getByRole('link', { name: 'Documents' })).toBeVisible()
  await expect(page.getByText('Either party may terminate the agreement')).toBeVisible()
  await expect(page.getByRole('toolbar', { name: 'Viewer' })).toBeVisible()
  const panel = page.getByRole('region', { name: 'Document intelligence' })
  await expect(panel.getByRole('tab', { name: 'AI insights' })).toHaveAttribute('aria-selected', 'true')
  await expect(panel.getByRole('region', { name: 'Suggested questions' })).toBeVisible()

  // "Ask about this document" focuses the question box.
  await page.getByRole('button', { name: 'Ask about this document' }).click()
  const box = panel.getByLabel('Ask about this document')
  await expect(box).toBeFocused()
  await box.fill('How much notice is needed to terminate the agreement?')
  await box.press('Enter')
  const answer = panel.getByRole('article', { name: 'Answer' }).last()
  await expect(answer).toContainText('ninety days', { timeout: 60_000 })
  await answer.getByRole('button', { name: /^Show source 1/ }).click()
  await expect(page.locator('mark.cite-highlight')).toContainText('ninety days')
  await expect(page).toHaveURL(/[?&]q=/)

  // The summary is generated on request, with sources.
  await panel.getByRole('button', { name: 'Summarize' }).click()
  await expect(panel.getByRole('region', { name: 'Document summary' }).getByRole('list', { name: 'Sources' })).toBeVisible({ timeout: 60_000 })

  // Metadata tab: details and processing figures.
  await panel.getByRole('tab', { name: 'Metadata' }).click()
  await expect(panel.getByText('Passages indexed')).toBeVisible()
  await expect(panel.getByText('vendor-policy.md')).toBeVisible()

  // No accessibility regressions in light or dark mode.
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme })
    await page.waitForTimeout(600)
    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
    expect(results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical').map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([])
  }
  await page.emulateMedia({ colorScheme: 'light' })

  // Zoom.
  const text = page.locator('.page-card__text').first()
  const before = await text.evaluate((el) => parseFloat(getComputedStyle(el).fontSize))
  await page.getByRole('button', { name: 'Larger text' }).click()
  await expect.poll(() => text.evaluate((el) => parseFloat(getComputedStyle(el).fontSize))).toBeGreaterThan(before)
  await expect(page.getByRole('group', { name: 'Zoom' })).toContainText('105%')

  // Hiding the panel is remembered; it comes back from the toolbar.
  await panel.getByRole('button', { name: 'Hide document panel' }).click()
  await expect(panel).toBeHidden()
  await page.reload()
  await expect(page.getByRole('region', { name: 'Document intelligence' })).toBeHidden()
  await page.getByRole('button', { name: 'Show panel' }).click()
  await expect(page.getByRole('region', { name: 'Document intelligence' })).toBeVisible()

  // Phone width: one column, no horizontal scroll.
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('multi-page documents get page thumbnails and page navigation', async ({ page, browser, baseURL }) => {
  test.setTimeout(180_000)
  // A real three-page PDF, printed by the browser.
  const printer = await browser.newPage()
  await printer.setContent(
    ['Scope', 'Payment', 'Termination']
      .map((h, i) => `<section style="page-break-after:always"><h1>${i + 1}. ${h}</h1><p>${h} terms for the thumbnail test. `.concat('Lorem ipsum dolor sit amet. '.repeat(20), '</p></section>'))
      .join(''),
  )
  const pdf = await printer.pdf({ format: 'A4' })
  await printer.close()

  await page.setViewportSize({ width: 1440, height: 900 })
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Tess Thumbs', uniqueEmail('thumbs'))
  const root = page.url().replace(/\/dashboard.*$/, '')
  await page.goto(`${root}/upload`)
  await page.locator('input[type=file]').last().setInputFiles({ name: 'three-pages.pdf', mimeType: 'application/pdf', buffer: pdf })
  await expect(page.getByText('Uploaded. Processing has started.')).toBeVisible()
  await page.goto(`${root}/documents`)
  const row = page.getByRole('row', { name: /three-pages/ })
  await expect(row.getByText('Ready')).toBeVisible({ timeout: 90_000 })
  await row.getByRole('link').first().click()

  const rail = page.getByRole('navigation', { name: 'Pages' })
  await expect(rail.getByRole('button')).toHaveCount(3)
  await expect(rail.getByRole('button', { name: 'Page 1' })).toHaveAttribute('aria-current', 'page')
  await rail.getByRole('button', { name: 'Page 3' }).click()
  await expect(page).toHaveURL(/[?&]page=3/)
  await expect(page.getByRole('toolbar', { name: 'Viewer' })).toContainText('3 / 3')
  await page.getByRole('button', { name: 'Previous page' }).click()
  await expect(page).toHaveURL(/[?&]page=2/)
  await page.screenshot({ path: 'test-results/viewer-layout.png' })
})
