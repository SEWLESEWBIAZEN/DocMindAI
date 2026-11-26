/**
 * Integrations (ADR 0037): the plan gate, creating and revoking an API key, and the
 * MCP endpoint answering with that key. The dev stack has no Slack app configured.
 *
 * Moving the workspace to Pro uses the operator CLI in the backend container
 * (E2E_BACKEND_CONTAINER, default documind-backend-1).
 */
import { execFileSync } from 'node:child_process'

import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

import { PASSWORD, registerAndSignIn, totp, uniqueEmail } from './support.ts'

const BACKEND = process.env.E2E_BACKEND_CONTAINER ?? 'documind-backend-1'

async function accessible(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
  expect(results.violations.map((v) => v.id)).toEqual([])
}

test('API keys and the MCP server', async ({ page, baseURL }) => {
  test.setTimeout(180_000)
  await registerAndSignIn(page, baseURL ?? 'http://localhost:5173', 'Ivy Integrate', uniqueEmail('integrate'))
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Integrations' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Integrations' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Integrations are included from the Pro plan' })).toBeVisible()
  await accessible(page)
  const workspace = new URL(page.url()).pathname.split('/')[2]

  execFileSync('docker', ['exec', BACKEND, 'documind-manage', 'plan', 'assign', workspace, 'pro', '--note', 'e2e'])

  // Creating a key needs 2FA.
  await page.getByRole('button', { name: /Account menu/ }).click()
  await page.getByRole('menuitem', { name: 'Security & 2FA' }).click()
  await page.getByRole('button', { name: 'Set up an authenticator app' }).click()
  await page.getByLabel('Confirm your password to continue').fill(PASSWORD)
  await page.getByRole('button', { name: 'Continue' }).click()
  const secret = (await page.getByLabel('Setup key').innerText()).replace(/\s/g, '')
  await page.getByLabel('Code from your app').fill(totp(secret))
  await page.getByRole('button', { name: 'Turn on 2FA' }).click()
  await page.getByRole('button', { name: "I've saved them" }).click()

  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Integrations' }).click()
  await page.reload() // the plan changed outside the app
  await expect(page.getByText("Slack isn't set up on this DocuMind server yet.")).toBeVisible()
  const mcpUrl = await page.getByLabel('MCP server URL').innerText()
  expect(mcpUrl).toContain(`/api/v1/workspaces/${workspace}/mcp`)
  await accessible(page)

  await page.getByRole('button', { name: 'New key' }).click()
  const form = page.getByRole('dialog', { name: 'New API key' })
  await form.getByLabel('Name').fill('Claude Desktop')
  await form.getByRole('button', { name: 'Create key' }).click()
  // A fresh TOTP window: the setup code's window may still be current.
  await page.waitForTimeout(31_000 - (Date.now() % 30_000))
  await page.getByLabel('Authentication code').fill(totp(secret))
  await page.getByRole('dialog', { name: 'Create API key' }).getByRole('button', { name: 'Create key' }).click()
  const shown = page.getByRole('dialog', { name: 'Copy your key now' })
  const token = (await shown.getByLabel('New API key').innerText()).trim()
  expect(token).toMatch(/^dmk_/)
  await shown.getByRole('button', { name: 'Done' }).click()
  await expect(page.getByRole('cell', { name: 'Claude Desktop', exact: true })).toBeVisible()
  await expect(page.getByText(token)).toHaveCount(0) // never shown again

  // The key works with the MCP server, without the browser session.
  const mcp = `/api/v1/workspaces/${workspace}/mcp`
  const init = await page.request.post(mcp, {
    headers: { Authorization: `Bearer ${token}` },
    data: { jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {} } },
  })
  expect(init.status()).toBe(200)
  expect((await init.json()).result.serverInfo.name).toBe('documind')
  const tools = await page.request.post(mcp, {
    headers: { Authorization: `Bearer ${token}` },
    data: { jsonrpc: '2.0', id: 2, method: 'tools/list' },
  })
  const names = ((await tools.json()).result.tools as { name: string }[]).map((t) => t.name)
  expect(names).toEqual(expect.arrayContaining(['search', 'ask', 'read_document']))

  await page.getByRole('button', { name: 'Revoke Claude Desktop' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Revoke' }).click()
  await expect(page.getByText('Key revoked.', { exact: false })).toBeVisible()
  const after = await page.request.post(mcp, {
    headers: { Authorization: `Bearer ${token}` },
    data: { jsonrpc: '2.0', id: 3, method: 'ping' },
  })
  expect(after.status()).toBe(401)
})
