/**
 * Pure timeline for the hero product demo: maps elapsed milliseconds to what
 * should be on screen. Keeping it pure makes the animation deterministic,
 * testable, and trivially "finished" for reduced-motion users.
 */
import type { AnswerPart, Scenario } from '../content'

export type DemoPhase = 'typing' | 'searching' | 'streaming' | 'reviewing'

export interface DemoFrame {
  phase: DemoPhase
  /** Characters of the question typed into the composer. */
  typedChars: number
  /** Whether the question has been sent as a message. */
  sent: boolean
  /** Number of answer tokens revealed (words and citation chips). */
  visibleTokens: number
  /** Citation currently highlighted in the source panel. */
  activeCitation: number | null
  /** True once the whole timeline (including the review hold) has elapsed. */
  finished: boolean
}

export type AnswerToken = { kind: 'word'; text: string } | { kind: 'cite'; id: number }

export const TIMING = {
  startDelay: 500,
  perChar: 26,
  sendPause: 380,
  searching: 1300,
  perToken: 52,
  perCitationReview: 1500,
} as const

export function tokenize(parts: AnswerPart[]): AnswerToken[] {
  return parts.flatMap((part): AnswerToken[] =>
    part.kind === 'cite'
      ? [{ kind: 'cite', id: part.id }]
      : (part.text.match(/\S+\s*|\s+/g) ?? []).map((text) => ({ kind: 'word', text })),
  )
}

interface Marks {
  typedAt: number
  sentAt: number
  streamAt: number
  reviewAt: number
  end: number
  tokens: AnswerToken[]
}

function marks(scenario: Scenario): Marks {
  const tokens = tokenize(scenario.answer)
  const typedAt = TIMING.startDelay + scenario.question.length * TIMING.perChar
  const sentAt = typedAt + TIMING.sendPause
  const streamAt = sentAt + TIMING.searching
  const reviewAt = streamAt + tokens.length * TIMING.perToken
  const end = reviewAt + scenario.citations.length * TIMING.perCitationReview
  return { typedAt, sentAt, streamAt, reviewAt, end, tokens }
}

export function demoDuration(scenario: Scenario): number {
  return marks(scenario).end
}

export function getDemoFrame(scenario: Scenario, elapsed: number): DemoFrame {
  const m = marks(scenario)
  const t = Math.max(0, elapsed)

  if (t < m.sentAt) {
    const typed = Math.floor((t - TIMING.startDelay) / TIMING.perChar)
    return {
      phase: 'typing',
      typedChars: Math.min(scenario.question.length, Math.max(0, typed)),
      sent: false,
      visibleTokens: 0,
      activeCitation: null,
      finished: false,
    }
  }
  if (t < m.streamAt) {
    return { phase: 'searching', typedChars: 0, sent: true, visibleTokens: 0, activeCitation: null, finished: false }
  }
  if (t < m.reviewAt) {
    const visible = Math.min(m.tokens.length, Math.floor((t - m.streamAt) / TIMING.perToken) + 1)
    // Highlight the most recent citation that has streamed in.
    let active: number | null = null
    for (let i = 0; i < visible; i++) {
      const token = m.tokens[i]
      if (token.kind === 'cite') active = token.id
    }
    return { phase: 'streaming', typedChars: 0, sent: true, visibleTokens: visible, activeCitation: active, finished: false }
  }
  // Review: walk the reader through each source in turn.
  const index = Math.min(
    scenario.citations.length - 1,
    Math.floor((t - m.reviewAt) / TIMING.perCitationReview),
  )
  return {
    phase: 'reviewing',
    typedChars: 0,
    sent: true,
    visibleTokens: m.tokens.length,
    activeCitation: scenario.citations[index]?.id ?? null,
    finished: t >= m.end,
  }
}

/** The settled end state, used for reduced motion and when the user takes control. */
export function finalFrame(scenario: Scenario, activeCitation?: number | null): DemoFrame {
  return {
    phase: 'reviewing',
    typedChars: 0,
    sent: true,
    visibleTokens: tokenize(scenario.answer).length,
    activeCitation: activeCitation ?? scenario.citations[0]?.id ?? null,
    finished: true,
  }
}
