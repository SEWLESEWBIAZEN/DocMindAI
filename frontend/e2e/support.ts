import { createHmac } from 'node:crypto'

import type { Page } from '@playwright/test'

export const MAILPIT = process.env.E2E_MAILPIT_URL ?? 'http://localhost:8025'
export const PASSWORD = 'correct horse battery staple'

interface MailSummary {
  ID: string
}

/** Wait for an email to `to` and return the first link matching `path`. */
export async function linkFromEmail(to: string, path: string, timeoutMs = 30_000): Promise<string> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const search = await fetch(`${MAILPIT}/api/v1/search?query=${encodeURIComponent(`to:${to}`)}`)
    const { messages } = (await search.json()) as { messages: MailSummary[] }
    for (const message of messages) {
      const body = (await (await fetch(`${MAILPIT}/api/v1/message/${message.ID}`)).json()) as { Text: string }
      const match = body.Text.match(new RegExp(`https?://[^\\s]*${path.replace(/[/]/g, '\\/')}\\?token=[\\w-]+`))
      if (match) return match[0]
    }
    await new Promise((resolve) => setTimeout(resolve, 400))
  }
  throw new Error(`No email with ${path} for ${to}`)
}

/** Make an email link point at the app under test (emails use PUBLIC_APP_URL). */
export function local(link: string, baseURL: string): string {
  const url = new URL(link)
  return `${baseURL.replace(/\/$/, '')}${url.pathname}${url.search}`
}

/** RFC 6238 TOTP (SHA-1, 6 digits, 30 s) for the 2FA flow. */
export function totp(secret: string, now = Date.now()): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
  const clean = secret.replace(/[\s=]/g, '').toUpperCase()
  let bits = ''
  for (const char of clean) bits += alphabet.indexOf(char).toString(2).padStart(5, '0')
  const bytes = Buffer.from(bits.match(/.{8}/g)?.map((b) => parseInt(b, 2)) ?? [])
  const counter = Buffer.alloc(8)
  counter.writeBigUInt64BE(BigInt(Math.floor(now / 1000 / 30)))
  const hmac = createHmac('sha1', bytes).update(counter).digest()
  const offset = (hmac[hmac.length - 1] ?? 0) & 0xf
  const code = (hmac.readUInt32BE(offset) & 0x7fffffff) % 1_000_000
  return code.toString().padStart(6, '0')
}

export function uniqueEmail(name: string): string {
  return `${name}.${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}@example.com`
}

export async function registerAndSignIn(page: Page, baseURL: string, name: string, email: string): Promise<void> {
  await page.goto('/register')
  await page.getByLabel('Name').fill(name)
  await page.getByLabel('Work email').fill(email)
  await page.getByLabel('Password', { exact: true }).fill(PASSWORD)
  await page.getByRole('button', { name: 'Create account' }).click()
  await page.getByRole('heading', { name: 'Check your email' }).waitFor()
  await page.goto(local(await linkFromEmail(email, '/verify-email'), baseURL))
  await page.getByRole('heading', { name: 'Email verified' }).waitFor()
  await page.getByRole('link', { name: 'Sign in' }).click()
  await page.getByRole('heading', { name: 'Sign in' }).waitFor()
  await page.getByLabel('Email', { exact: true }).fill(email)
  await page.getByLabel('Password', { exact: true }).fill(PASSWORD)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await page.waitForURL(/\/w\/[^/]+\/dashboard/)
}
