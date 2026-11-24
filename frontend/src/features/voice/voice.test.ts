import { afterEach, describe, expect, it, vi } from 'vitest'

import { describeWarning } from '../documents/format'
import { clampRate, getPreferences, resetPreferencesForTests, setPreferences } from './settings'
import { Speaker, type SpeakOptions } from './speaker'
import {
  answerToSpeech,
  completedSentences,
  nextSectionIndex,
  pageToSpeech,
  sourcesSummary,
  splitSentences,
} from './text'
import { chooseEngine } from './VoiceContext'

const CITES = [
  { number: 1, spoken_label: 'Master Services Agreement, page 14' },
  { number: 2, spoken_label: 'Vendor Policy, pages 3 to 4' },
]

describe('splitSentences', () => {
  it('keeps abbreviations, initials and decimals together', () => {
    expect(splitSentences('Revenue rose 4.2 percent, e.g. in Q3. See p. 14 for details! Done?')).toEqual([
      'Revenue rose 4.2 percent, e.g. in Q3.',
      'See p. 14 for details!',
      'Done?',
    ])
    expect(splitSentences('  ')).toEqual([])
  })
})

describe('answerToSpeech', () => {
  it('speaks sources in place of markers and strips Markdown', () => {
    expect(
      answerToSpeech('**Notice** is ninety days [1]. Large renewals need *Legal* review [1, 2].', CITES, {
        speakCitations: true,
      }),
    ).toEqual([
      'Notice is ninety days. (Source: Master Services Agreement, page 14.)',
      'Large renewals need Legal review. (Source: Master Services Agreement, page 14; Vendor Policy, pages 3 to 4.)',
    ])
  })

  it('keeps a marker placed after the full stop with its sentence', () => {
    expect(answerToSpeech('Ninety days notice. [1] Legal reviews it. [2]', CITES, { speakCitations: true })).toEqual([
      'Ninety days notice. (Source: Master Services Agreement, page 14.)',
      'Legal reviews it. (Source: Vendor Policy, pages 3 to 4.)',
    ])
  })

  it('drops markers when sources are not spoken', () => {
    expect(answerToSpeech('Ninety days [1].', CITES, { speakCitations: false })).toEqual(['Ninety days.'])
  })

  it('summarises sources after a streamed answer', () => {
    expect(sourcesSummary(CITES)).toBe('Sources. 1: Master Services Agreement, page 14. 2: Vendor Policy, pages 3 to 4.')
    expect(sourcesSummary([])).toBeNull()
  })
})

describe('completedSentences', () => {
  it('returns finished sentences as text streams in', () => {
    let state = completedSentences('Notice is ninety', 0)
    expect(state.sentences).toEqual([])
    state = completedSentences('Notice is ninety days [1]. Large renew', state.offset)
    expect(state.sentences).toEqual(['Notice is ninety days.'])
    const rest = 'Notice is ninety days [1]. Large renewals need review. Fees'
    const next = completedSentences(rest, state.offset)
    expect(next.sentences).toEqual(['Large renewals need review.'])
    expect(rest.slice(next.offset)).toBe('Fees')
  })
})

describe('document reading helpers', () => {
  const pages = [
    { position: 0, page_number: 1, text: 'Intro\nFirst point. More intro.', headings: ['Intro'], ocr: false },
    { position: 1, page_number: 2, text: 'Still intro.', headings: ['Intro'], ocr: false },
    { position: 2, page_number: 3, text: 'Fees apply.', headings: ['Fees'], ocr: false },
  ]
  it('reads the page number first', () => {
    expect(pageToSpeech(pages[0]!)).toEqual(['Page 1.', 'Intro', 'First point.', 'More intro.'])
  })
  it('finds the next section', () => {
    expect(nextSectionIndex(pages, 0)).toBe(2)
    expect(nextSectionIndex(pages, 2)).toBeNull()
  })
})

describe('preferences', () => {
  afterEach(() => resetPreferencesForTests())
  it('are off by default, clamp the rate and persist', () => {
    expect(getPreferences().enabled).toBe(false)
    expect(getPreferences().autoRead).toBe(false)
    setPreferences({ enabled: true, rate: 7 })
    expect(getPreferences().rate).toBe(2)
    expect(JSON.parse(localStorage.getItem('documind.voice.v1') ?? '{}')).toMatchObject({ enabled: true, rate: 2 })
    expect(clampRate(0.1)).toBe(0.5)
  })
})

describe('chooseEngine', () => {
  it('honours the preference and falls back', () => {
    expect(chooseEngine('auto', true, true)).toBe('server')
    expect(chooseEngine('auto', false, true)).toBe('browser')
    expect(chooseEngine('server', false, true)).toBe('browser')
    expect(chooseEngine('browser', true, false)).toBe('server')
    expect(chooseEngine('auto', false, false)).toBeNull()
  })
})

class FakeEngine {
  spoken: { text: string; rate: number }[] = []
  pending: (() => void)[] = []
  paused = false
  cancelled = false

  speak(text: string, rate: number): Promise<void> {
    this.spoken.push({ text, rate })
    return new Promise((resolve) => this.pending.push(resolve))
  }
  pause() {
    this.paused = true
  }
  resume() {
    this.paused = false
  }
  cancel() {
    this.cancelled = true
  }
  finishOne() {
    this.pending.shift()?.()
  }
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0))

function setup() {
  const engine = new FakeEngine()
  const speaker = new Speaker(() => engine)
  const options = (extra: Partial<SpeakOptions> = {}): SpeakOptions => ({ label: 'answer', rate: 1, engine: 'browser', ...extra })
  return { engine, speaker, options }
}

describe('Speaker', () => {
  it('reads sentences in order and reports progress', async () => {
    const { engine, speaker, options } = setup()
    const onSentence = vi.fn()
    const onEnd = vi.fn()
    speaker.play(['One.', 'Two.'], options({ onSentence, onEnd }))
    await tick()
    expect(speaker.getState()).toMatchObject({ status: 'speaking', index: 0, total: 2, sentence: 'One.' })
    engine.finishOne()
    await tick()
    expect(onSentence).toHaveBeenLastCalledWith('Two.', 1)
    engine.finishOne()
    await tick()
    expect(speaker.getState().status).toBe('idle')
    expect(onEnd).toHaveBeenCalledOnce()
    expect(engine.spoken.map((s) => s.text)).toEqual(['One.', 'Two.'])
  })

  it('pauses between sentences, resumes and applies a new rate', async () => {
    const { engine, speaker, options } = setup()
    speaker.play(['One.', 'Two.'], options())
    await tick()
    speaker.pause()
    expect(engine.paused).toBe(true)
    engine.finishOne()
    await tick()
    expect(engine.spoken).toHaveLength(1) // waits while paused
    speaker.setRate(1.5)
    speaker.resume()
    await tick()
    expect(engine.spoken[1]).toEqual({ text: 'Two.', rate: 1.5 })
  })

  it('stops instantly and never calls onEnd', async () => {
    const { engine, speaker, options } = setup()
    const onEnd = vi.fn()
    speaker.play(['One.', 'Two.'], options({ onEnd }))
    await tick()
    speaker.stop()
    engine.finishOne()
    await tick()
    expect(engine.cancelled).toBe(true)
    expect(speaker.getState().status).toBe('idle')
    expect(engine.spoken).toHaveLength(1)
    expect(onEnd).not.toHaveBeenCalled()
  })

  it('reads a stream as sentences are appended, then finishes', async () => {
    const { engine, speaker, options } = setup()
    const onEnd = vi.fn()
    speaker.openStream(options({ onEnd }))
    await tick()
    expect(speaker.getState().status).toBe('speaking')
    speaker.append(['First.'])
    await tick()
    expect(engine.spoken.map((s) => s.text)).toEqual(['First.'])
    engine.finishOne()
    speaker.append(['Second.'])
    await tick()
    speaker.closeStream()
    engine.finishOne()
    await tick()
    expect(engine.spoken.map((s) => s.text)).toEqual(['First.', 'Second.'])
    expect(onEnd).toHaveBeenCalledOnce()
  })

  it('starting new speech replaces the old', async () => {
    const { engine, speaker, options } = setup()
    speaker.play(['Old.'], options())
    await tick()
    speaker.play(['New.'], options({ label: 'other' }))
    await tick()
    expect(engine.spoken.map((s) => s.text)).toEqual(['Old.', 'New.'])
    expect(speaker.getState().label).toBe('other')
  })
})

describe('extraction notes', () => {
  it('describe figure results in plain language', () => {
    expect(describeWarning('figures_described:count=2')).toMatch(/2 figures described/)
    expect(describeWarning('figure_descriptions_failed:count=1')).toMatch(/1 figure couldn't/)
    expect(describeWarning('ocr_applied:pages=2,5')).toBe('Text recognition (OCR) was used on pages 2, 5.')
  })
})
