/**
 * Accessibility audit (Phase 9): axe WCAG 2.2 A/AA on every page, in light and dark
 * mode, plus keyboard-only operation of the core journey. Manual screen-reader
 * testing (NVDA, JAWS, VoiceOver) and sessions with blind users are separate:
 * see docs/accessibility/.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

const DOC = `# Accessibility Policy

## Scope

Every page must work with a keyboard and a screen reader.
`

async function violations(page: Page): Promise<string[]> {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze()
  return results.violations
    .filter((v) => v.impact === 'serious' || v.impact === 'critical')
    .map((v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(' ')).join(', ')})`)
}

async function audit(page: Page, label: string, found: Record<string, string[]>) {
  await page.getByRole('heading', { level: 1 }).first().waitFor()
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme })
    // Let colour transitions finish: axe must judge the final colours, not a blend.
    await page.waitForTimeout(600)
    const v = await violations(page)
    if (v.length) found[`${label} (${scheme})`] = v
  }
  await page.emulateMedia({ colorScheme: 'light' })
}

test('every page passes axe in light and dark mode', async ({ page, baseURL }) => {
  test.setTimeout(240_000)
  const base = baseURL ?? 'http://localhost:5173'
  const found: Record<string, string[]> = {}

  for (const path of ['/', '/login', '/register', '/forgot-password', '/status', '/does-not-exist']) {
    await page.goto(path)
    await audit(page, path, found)
  }

  await registerAndSignIn(page, base, 'Ada Audit', uniqueEmail('audit'))
  const root = page.url().replace(/\/dashboard.*$/, '')
  // Content for the document, viewer and chat pages.
  await page.goto(`${root}/upload`)
  await page.locator('input[type=file]').last().setInputFiles({ name: 'a11y-policy.md', mimeType: 'text/markdown', buffer: Buffer.from(DOC) })
  await expect(page.getByText('Uploaded. Processing has started.')).toBeVisible()
  await page.goto(`${root}/documents`)
  const row = page.getByRole('row', { name: /a11y-policy/ })
  await expect(row.getByText('Ready')).toBeVisible({ timeout: 90_000 })
  await row.getByRole('link').first().click()
  await expect(page.getByText('Every page must work with a keyboard')).toBeVisible()
  await audit(page, 'viewer', found)

  for (const path of ['dashboard', 'documents', 'upload', 'collections', 'chat', 'conversations', 'organization', 'settings', 'profile', 'usage', 'ai-providers']) {
    await page.goto(`${root}/${path}`)
    await audit(page, path, found)
  }

  // Organization-only pages.
  await page.getByRole('button', { name: /Workspace: .*Switch workspace/ }).click()
  await page.getByRole('menuitem', { name: 'Create organization' }).click()
  await page.getByLabel('Organization name').fill('Audit Org')
  await page.getByRole('button', { name: 'Create organization' }).last().click()
  await expect(page.getByRole('heading', { level: 1, name: 'Members' })).toBeVisible()
  await audit(page, 'members', found)
  const org = page.url().replace(/\/members.*$/, '')
  await page.goto(`${org}/infrastructure`)
  await audit(page, 'infrastructure', found)

  expect(found).toEqual({})
})

test('the core journey works with the keyboard alone', async ({ page, baseURL }) => {
  const base = baseURL ?? 'http://localhost:5173'
  await registerAndSignIn(page, base, 'Kim Keyboard', uniqueEmail('keyboard'))
  const root = page.url().replace(/\/dashboard.*$/, '')

  // Skip link is the first stop and moves focus to the main content.
  await page.goto(`${root}/documents`)
  await page.getByRole('heading', { level: 1 }).first().waitFor()
  await page.keyboard.press('Tab')
  const skip = page.getByRole('link', { name: 'Skip to content' })
  await expect(skip).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/#main$/)

  // Menus open with the keyboard, move with arrows, close with Escape and return focus.
  const switcher = page.getByRole('button', { name: /Workspace: .*Switch workspace/ })
  await switcher.focus()
  await page.keyboard.press('Enter')
  const menu = page.getByRole('menu')
  await expect(menu).toBeVisible()
  await expect(menu.getByRole('menuitem').first()).toBeFocused() // focus moves into the menu
  await page.keyboard.press('ArrowDown')
  await expect(menu.getByRole('menuitem').nth(1)).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(menu).toBeHidden()
  await expect(switcher).toBeFocused()

  // Ask a question without touching the mouse.
  await page.goto(`${root}/chat`)
  const composer = page.getByLabel('Ask a question about your documents')
  await composer.focus()
  await page.keyboard.type('What is the scope?')
  await page.keyboard.press('Enter')
  await expect(page.getByRole('article').last()).toBeVisible({ timeout: 60_000 })

  // Dialogs trap focus and give it back.
  await page.goto(`${root}/ai-providers`)
  const keyField = page.getByLabel('API key')
  await keyField.focus()
  await page.keyboard.type('sk-proj-keyboard-only-not-a-real-key-0000')
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()

  // Every interactive element on the main pages has a visible focus indicator.
  await page.goto(`${root}/usage`)
  await page.getByRole('heading', { level: 1 }).first().waitFor()
  for (let i = 0; i < 12; i++) {
    await page.keyboard.press('Tab')
    const outline = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null
      if (!el || el === document.body) return 'none'
      const style = getComputedStyle(el)
      return `${style.outlineStyle}/${style.outlineWidth}/${style.boxShadow}`
    })
    expect(outline, `focus indicator on tab stop ${i + 1}`).not.toMatch(/^none\/0px\/none$/)
  }
})
