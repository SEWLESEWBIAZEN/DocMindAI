import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

import { PASSWORD, codeFromEmail, linkFromEmail, local, registerAndSignIn, totp, uniqueEmail } from './support.ts'

const SHOTS = process.env.E2E_SCREENSHOTS

async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: false })
}

/** WCAG 2.x A/AA scan of the current view; fails on serious or critical issues. */
async function expectAccessible(page: Page, label: string) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze()
  const serious = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical')
  expect(
    serious.map((v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(' ')).join(', ')})`),
    `accessibility violations on ${label}`,
  ).toEqual([])
}

const POLICY = `# Vendor Policy

## Approvals

Vendor renewals above fifty thousand dollars require Legal review before signature.

## Security

Every vendor handling customer data must complete the annual security questionnaire.

## Termination

Either party may terminate the agreement with ninety days written notice.
`

test('complete journey: sign up, upload, ask with citations, team, 2FA', async ({ page, browser, baseURL }) => {
  const base = baseURL ?? 'http://localhost:5173'
  const owner = uniqueEmail('owner')

  // --- account ------------------------------------------------------------
  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  await page.goto('/login')
  await expectAccessible(page, 'login')
  await shot(page, '01-login')
  await registerAndSignIn(page, base, 'Olivia Owner', owner)
  await expect(page.getByRole('heading', { level: 1 })).toContainText('Olivia')
  await expectAccessible(page, 'dashboard')
  await shot(page, '02-dashboard-empty')

  // --- upload and processing ----------------------------------------------
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Upload' }).click()
  await expect(page.getByRole('heading', { name: 'Upload documents' })).toBeVisible()
  await page.locator('input[type=file]').last().setInputFiles({
    name: 'vendor-policy.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from(POLICY),
  })
  await expect(page.getByText('Uploaded. Processing has started.')).toBeVisible()
  await page.getByRole('link', { name: 'View all documents' }).click()
  const row = page.getByRole('row', { name: /vendor-policy/ })
  await expect(row.getByText('Ready')).toBeVisible({ timeout: 90_000 })
  await expectAccessible(page, 'documents')
  await shot(page, '03-documents')

  // --- viewer ---------------------------------------------------------------
  await row.getByRole('link').first().click()
  await expect(page.getByText('Either party may terminate the agreement')).toBeVisible()
  await shot(page, '04-viewer')

  // --- chat: stream, citations, source navigation -----------------------------
  await page.getByRole('button', { name: 'Ask about this' }).click()
  await expect(page.getByRole('button', { name: /Sources: vendor-policy/ })).toBeVisible()
  const composer = page.getByLabel('Ask a question about your documents')
  await composer.fill('How much notice is needed to terminate the agreement?')
  await composer.press('Enter')
  const answer = page.getByRole('article', { name: 'Answer' }).last()
  await expect(answer).toContainText('ninety days', { timeout: 60_000 })
  await expect(answer.getByRole('button', { name: 'Source 1' })).toBeVisible()
  await expect(answer.getByText('vendor-policy, section Termination')).toBeVisible()
  await page.waitForURL(/\/chat\/[0-9a-f-]{36}$/)
  await expectAccessible(page, 'chat')
  await shot(page, '05-chat-answer')

  // follow-up in the same conversation
  await composer.fill('And which renewals require Legal review?')
  await page.getByRole('button', { name: 'Send question' }).click()
  await expect(page.getByRole('article', { name: 'Answer' }).last()).toContainText('fifty thousand', { timeout: 60_000 })

  // regenerate the latest answer
  await page.getByRole('button', { name: 'Regenerate' }).click()
  await expect(page.getByRole('article', { name: 'Answer' })).toHaveCount(2, { timeout: 60_000 })

  // source navigation: open the cited passage, highlighted in the viewer
  await page.getByRole('article', { name: 'Answer' }).first().getByRole('link', { name: /Open vendor-policy/ }).click()
  await expect(page.locator('mark.cite-highlight')).toContainText('terminate the agreement')
  await shot(page, '06-source-highlight')

  // conversation history
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Conversations' }).click()
  await expect(page.getByRole('link', { name: 'How much notice is needed to terminate the agreement?' })).toBeVisible()
  await expectAccessible(page, 'conversations')

  // --- organization and members --------------------------------------------
  await page.getByRole('button', { name: /Workspace: .*Switch workspace/ }).click()
  await page.getByRole('menuitem', { name: 'Create organization' }).click()
  await page.getByLabel('Organization name').fill('Acme Legal')
  await page.getByRole('button', { name: 'Create organization' }).last().click()
  await expect(page.getByRole('heading', { level: 1, name: 'Members' })).toBeVisible()
  const colleague = uniqueEmail('colleague')
  await page.getByLabel('Email address').fill(colleague)
  await page.getByLabel('Role').selectOption('viewer')
  await page.getByRole('button', { name: 'Send invitation' }).click()
  await expect(page.getByText(colleague, { exact: true })).toBeVisible()
  await expectAccessible(page, 'members')
  await shot(page, '07-members')

  // colleague signs up in a separate browser and accepts the invitation link
  const other = await browser.newContext({ baseURL: base })
  const colleaguePage = await other.newPage()
  await registerAndSignIn(colleaguePage, base, 'Casey Colleague', colleague)
  await colleaguePage.goto(local(await linkFromEmail(colleague, '/invitations/accept'), base))
  await colleaguePage.getByRole('button', { name: 'Accept invitation' }).click()
  await colleaguePage.waitForURL(/\/w\/[^/]+\/dashboard/)
  await expect(colleaguePage.getByRole('button', { name: /Workspace: Acme Legal/ })).toBeVisible()
  // Viewers can't upload: the action isn't offered.
  await expect(colleaguePage.getByRole('button', { name: 'Upload' })).toHaveCount(0)
  await other.close()

  await page.reload()
  await expect(page.getByRole('cell', { name: /^Casey Colleague/ })).toBeVisible()

  // --- usage and settings (owner) ---------------------------------------------
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Usage' }).click()
  await expect(page.getByText('Questions per day')).toBeVisible()
  await expectAccessible(page, 'usage')
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Settings' }).click()
  await expect(page.getByRole('heading', { name: 'Audit log' })).toBeVisible()

  // --- 2FA, then a 2FA-protected delete in the personal workspace ---------------
  await page.getByRole('button', { name: /Workspace: Acme Legal/ }).click()
  await page.getByRole('menuitem', { name: /\(personal\)/ }).click()
  await page.getByRole('button', { name: /Account menu/ }).click()
  await page.getByRole('menuitem', { name: 'Security & 2FA' }).click()
  await page.getByRole('button', { name: 'Set up an authenticator app' }).click()
  await page.getByLabel('Confirm your password to continue').fill(PASSWORD)
  await page.getByRole('button', { name: 'Continue' }).click()
  const secret = (await page.getByLabel('Setup key').innerText()).replace(/\s/g, '')
  await page.getByLabel('Code from your app').fill(totp(secret))
  await page.getByRole('button', { name: 'Turn on 2FA' }).click()
  await expect(page.getByRole('list', { name: 'Recovery codes' })).toBeVisible()
  await shot(page, '08-recovery-codes')
  await page.getByRole('button', { name: "I've saved them" }).click()

  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Documents' }).click()
  await page.getByRole('button', { name: 'Actions for vendor-policy' }).click()
  await page.getByRole('menuitem', { name: 'Delete permanently…' }).click()
  // Wait for a fresh TOTP window if the setup code's window is still current.
  await page.waitForTimeout(31_000 - (Date.now() % 30_000))
  await page.getByLabel('Authentication code').fill(totp(secret))
  await page.getByRole('button', { name: 'Delete permanently' }).click()
  await expect(page.getByText('Document deleted.')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'No documents yet' })).toBeVisible()

  // --- sign out -------------------------------------------------------------------
  await page.getByRole('button', { name: /Account menu/ }).click()
  await page.getByRole('menuitem', { name: 'Sign out' }).click()
  await page.waitForURL('**/login')
  await page.goto('/app')
  await page.waitForURL(/\/login\?next=/)

  // --- sign in again: 2FA is on, so a code is needed after the password (ADR 0026) ---
  await page.getByLabel('Email').fill(owner)
  await page.getByLabel('Password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Two-factor authentication' })).toBeVisible()
  await expectAccessible(page, 'sign-in code')
  await page.getByLabel('Authentication code').fill('000000')
  await page.getByRole('button', { name: 'Verify and sign in' }).click()
  await expect(page.locator('.alert--error')).toContainText("isn't valid")
  // Without the phone: a one-time code by email (ADR 0027).
  await page.getByRole('button', { name: 'Email me a code instead' }).click()
  await expect(page.getByText('We sent a one-time code to your email address')).toBeVisible()
  await page.getByLabel('Email code').fill(await codeFromEmail(owner))
  await page.getByRole('button', { name: 'Verify and sign in' }).click()
  await page.waitForURL(/\/w\/.+\/dashboard/)
})

test('protected routes redirect to sign-in and back', async ({ page }) => {
  await page.goto('/w/00000000-0000-0000-0000-000000000000/documents')
  await page.waitForURL(/\/login\?next=%2Fw%2F/)
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible()
})

test('mobile layout: navigation drawer and no horizontal scroll', async ({ browser, baseURL }) => {
  const base = baseURL ?? 'http://localhost:5173'
  const context = await browser.newContext({ baseURL: base, viewport: { width: 390, height: 844 }, deviceScaleFactor: 1 })
  const page = await context.newPage()
  await registerAndSignIn(page, base, 'Mo Bile', uniqueEmail('mobile'))
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(0)
  await page.getByRole('button', { name: 'Open navigation' }).click()
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Documents' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Documents', exact: true })).toBeVisible()
  await shot(page, '09-mobile-documents')
  for (const path of ['chat', 'conversations', 'collections', 'profile', 'settings']) {
    await page.goto(page.url().replace(/\/documents.*$/, `/${path}`))
    await page.getByRole('heading', { level: 1 }).first().waitFor()
    const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(wide, `horizontal overflow on ${path}`).toBeLessThanOrEqual(0)
  }
  await context.close()
})
