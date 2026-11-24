import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

import { registerAndSignIn, uniqueEmail } from './support.ts'

/**
 * Voice journey (ADR 0011). Headless Chromium has no voices or microphone, so
 * speechSynthesis and SpeechRecognition are replaced with recorders: the app
 * code paths are real, only the audio is simulated.
 */
const SPEECH_STUBS = () => {
  const w = window as unknown as Record<string, unknown>
  const spoken: string[] = []
  w.__spoken = spoken
  w.__transcript = 'How much notice is needed to terminate the agreement?'
  const synth = {
    speaking: false,
    paused: false,
    getVoices: () => [],
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    speak(utterance: SpeechSynthesisUtterance) {
      spoken.push(utterance.text)
      setTimeout(() => utterance.onend?.(new Event("end") as SpeechSynthesisEvent), 500)
    },
    cancel() {},
    pause() {},
    resume() {},
  }
  Object.defineProperty(window, 'speechSynthesis', { value: synth, configurable: true })
  class FakeRecognition {
    lang = ''
    interimResults = false
    continuous = false
    maxAlternatives = 1
    onresult: ((e: unknown) => void) | null = null
    onerror: ((e: unknown) => void) | null = null
    onend: (() => void) | null = null
    start() {
      setTimeout(() => {
        this.onresult?.({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: String(w.__transcript) } }] })
        this.onend?.()
      }, 150)
    }
    stop() {}
    abort() {
      this.onend?.()
    }
  }
  w.SpeechRecognition = FakeRecognition
  w.webkitSpeechRecognition = FakeRecognition
}

const POLICY = `# Vendor Policy

## Approvals

Vendor renewals above fifty thousand dollars require Legal review before signature.

## Termination

Either party may terminate the agreement with ninety days written notice.
`

const spoken = (page: Page) => page.evaluate(() => (window as unknown as { __spoken: string[] }).__spoken.slice())
const clearSpoken = (page: Page) => page.evaluate(() => ((window as unknown as { __spoken: string[] }).__spoken.length = 0))

test('voice: dictate, hear cited answers, listen to documents, spoken confirmations, policy', async ({ page, baseURL }) => {
  const base = baseURL ?? 'http://localhost:5173'
  await page.addInitScript(SPEECH_STUBS)
  await registerAndSignIn(page, base, 'Vera Voice', uniqueEmail('voice'))
  const nav = page.getByRole('navigation', { name: 'Main' })

  // Voice is off until the user turns it on: no voice buttons anywhere.
  await nav.getByRole('link', { name: 'Ask' }).click()
  await expect(page.getByRole('button', { name: 'Ask by voice' })).toHaveCount(0)

  await nav.getByRole('link', { name: 'Upload' }).click()
  await page.locator('input[type=file]').last().setInputFiles({ name: 'vendor-policy.md', mimeType: 'text/markdown', buffer: Buffer.from(POLICY) })
  await page.getByRole('link', { name: 'View all documents' }).click()
  await expect(page.getByRole('row', { name: /vendor-policy/ }).getByText('Ready')).toBeVisible({ timeout: 90_000 })

  // Opt in (Profile → Voice and reading).
  await page.getByRole('button', { name: /Account menu/ }).click()
  await page.getByRole('menuitem', { name: 'Profile' }).click()
  await page.getByRole('checkbox', { name: /Use voice features/ }).check()
  await page.getByRole('checkbox', { name: /Read answers aloud as they arrive/ }).check()
  await expect(page.getByLabel('Voice engine')).toHaveValue('auto')
  const results = await new AxeBuilder({ page }).include('#voice').withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze()
  expect(results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical').map((v) => v.id)).toEqual([])
  await page.getByRole('button', { name: 'Test voice' }).click()
  await expect.poll(() => spoken(page)).toContain('This is how answers will sound.')
  await expect(page.getByRole('region', { name: 'Reading controls' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('region', { name: 'Reading controls' })).toHaveCount(0)

  // Ask by voice: the transcript lands in the box for review, Enter sends it.
  await nav.getByRole('link', { name: 'Ask' }).click()
  await clearSpoken(page)
  await page.getByRole('button', { name: 'Ask by voice' }).click()
  const composer = page.getByLabel('Ask a question about your documents')
  await expect(composer).toHaveValue('How much notice is needed to terminate the agreement?')
  await composer.press('Enter')
  const answer = page.getByRole('article', { name: 'Answer' }).last()
  await expect(answer).toContainText('ninety days', { timeout: 60_000 })
  // Read aloud as it arrived, followed by the sources.
  await expect.poll(() => spoken(page)).toContain('Either party may terminate the agreement with ninety days written notice.')
  await expect.poll(() => spoken(page)).toContain('Sources. 1: vendor-policy, section Termination.')

  // Listen again later, with the source spoken next to the statement.
  await clearSpoken(page)
  await answer.getByRole('button', { name: 'Listen' }).click()
  await expect
    .poll(() => spoken(page))
    .toContain('Either party may terminate the agreement with ninety days written notice. (Source: vendor-policy, section Termination.)')

  // Keyboard: Ctrl+Shift+Space starts voice input when nothing is being read.
  await page.evaluate(() => ((window as unknown as { __transcript: string }).__transcript = 'Who reviews large renewals?'))
  await expect(page.getByRole('region', { name: 'Reading controls' })).toHaveCount(0)
  await page.keyboard.press('Control+Shift+Space')
  await expect(composer).toHaveValue('Who reviews large renewals?')

  // Listening mode in the document viewer.
  await nav.getByRole('link', { name: 'Documents' }).click()
  await page.getByRole('row', { name: /vendor-policy/ }).getByRole('link').first().click()
  await page.getByRole('button', { name: 'Listen' }).click()
  await clearSpoken(page)
  await page.getByRole('button', { name: 'Read', exact: true }).click()
  await expect.poll(() => spoken(page)).toContain('Either party may terminate the agreement with ninety days written notice.')
  await page.getByRole('button', { name: 'Summarize document' }).click()
  await expect(page.getByRole('region', { name: 'Summary' })).toBeVisible()
  await expect.poll(() => spoken(page)).toContain('Summary of the text.')

  // Spoken confirmation for a destructive action.
  await nav.getByRole('link', { name: 'Documents' }).click()
  await clearSpoken(page)
  await page.getByRole('button', { name: 'Actions for vendor-policy' }).click()
  await page.getByRole('menuitem', { name: 'Archive' }).click()
  await expect.poll(async () => (await spoken(page)).join(' ')).toContain('Archive "vendor-policy"?')
  await page.getByRole('button', { name: 'Cancel' }).click()

  // An admin turns voice off for the workspace: the voice controls disappear.
  await nav.getByRole('link', { name: 'Settings' }).click()
  await page.getByRole('radio', { name: /^Off/ }).check()
  await expect(page.getByText('Voice policy updated.')).toBeVisible()
  await nav.getByRole('link', { name: 'Ask' }).click()
  await expect(page.getByLabel('Ask a question about your documents')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Ask by voice' })).toHaveCount(0)
})
