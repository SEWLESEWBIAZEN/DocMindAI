import { describe, expect, it } from 'vitest'

import { parseAnswer, SCENARIOS, type Scenario } from '../content'
import { demoDuration, finalFrame, getDemoFrame, TIMING, tokenize } from './timeline'

const scenario: Scenario = {
  ...SCENARIOS[0],
  question: 'Hello?',
  answer: parseAnswer('Yes [1] and no [2].'),
  citations: [
    { id: 1, document: 'A', page: 1, excerpt: 'a' },
    { id: 2, document: 'B', page: 2, excerpt: 'b' },
  ],
}

const typedAt = TIMING.startDelay + scenario.question.length * TIMING.perChar
const streamAt = typedAt + TIMING.sendPause + TIMING.searching

describe('parseAnswer / tokenize', () => {
  it('splits text and citation markers', () => {
    expect(parseAnswer('A [1] b [12].')).toEqual([
      { kind: 'text', text: 'A ' },
      { kind: 'cite', id: 1 },
      { kind: 'text', text: ' b ' },
      { kind: 'cite', id: 12 },
      { kind: 'text', text: '.' },
    ])
  })

  it('tokenizes words and citations, preserving all text', () => {
    const tokens = tokenize(scenario.answer)
    expect(tokens.filter((t) => t.kind === 'cite')).toHaveLength(2)
    const text = tokens.map((t) => (t.kind === 'word' ? t.text : `[${t.id}]`)).join('')
    expect(text).toBe('Yes [1] and no [2].')
  })

  it('every shipped scenario cites only citations it defines', () => {
    for (const s of SCENARIOS) {
      const ids = new Set(s.citations.map((c) => c.id))
      const cited = s.answer.flatMap((p) => (p.kind === 'cite' ? [p.id] : []))
      expect(cited.length).toBeGreaterThan(0)
      cited.forEach((id) => expect(ids.has(id)).toBe(true))
    }
  })
})

describe('getDemoFrame', () => {
  it('starts with an empty composer', () => {
    expect(getDemoFrame(scenario, 0)).toMatchObject({ phase: 'typing', typedChars: 0, sent: false })
  })

  it('types the question character by character', () => {
    const frame = getDemoFrame(scenario, TIMING.startDelay + 3 * TIMING.perChar)
    expect(frame).toMatchObject({ phase: 'typing', typedChars: 3 })
    expect(getDemoFrame(scenario, typedAt).typedChars).toBe(scenario.question.length)
  })

  it('sends, then searches before answering', () => {
    expect(getDemoFrame(scenario, typedAt + TIMING.sendPause)).toMatchObject({
      phase: 'searching',
      sent: true,
      visibleTokens: 0,
    })
  })

  it('streams tokens and highlights the latest citation', () => {
    expect(getDemoFrame(scenario, streamAt)).toMatchObject({ phase: 'streaming', visibleTokens: 1, activeCitation: null })
    // Tokens: "Yes ", [1], " ", ... -> second token is citation 1.
    expect(getDemoFrame(scenario, streamAt + TIMING.perToken).activeCitation).toBe(1)
  })

  it('reviews each citation in turn, then finishes', () => {
    const reviewAt = streamAt + tokenize(scenario.answer).length * TIMING.perToken
    expect(getDemoFrame(scenario, reviewAt)).toMatchObject({ phase: 'reviewing', activeCitation: 1, finished: false })
    expect(getDemoFrame(scenario, reviewAt + TIMING.perCitationReview).activeCitation).toBe(2)
    expect(getDemoFrame(scenario, demoDuration(scenario)).finished).toBe(true)
  })

  it('final frame shows the full answer', () => {
    expect(finalFrame(scenario)).toMatchObject({
      visibleTokens: tokenize(scenario.answer).length,
      activeCitation: 1,
      finished: true,
    })
    expect(finalFrame(scenario, 2).activeCitation).toBe(2)
  })
})
