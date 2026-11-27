import { describe, expect, it } from 'vitest'

import { STARTERS, citationPlace } from './intelligence'

describe('citationPlace', () => {
  it('shows the page and the closest heading', () => {
    expect(citationPlace({ page_start: 12, page_end: 12, heading_path: ['Security', 'MFA'] })).toBe('p. 12 · MFA')
  })

  it('shows a page range', () => {
    expect(citationPlace({ page_start: 3, page_end: 5, heading_path: [] })).toBe('pp. 3–5')
  })

  it('falls back to the section, then to "Passage", for text without pages', () => {
    expect(citationPlace({ page_start: null, page_end: null, heading_path: ['Termination'] })).toBe('Termination')
    expect(citationPlace({ page_start: null, page_end: null, heading_path: [] })).toBe('Passage')
  })
})

describe('STARTERS', () => {
  it('offers a few short, distinct questions', () => {
    expect(new Set(STARTERS).size).toBe(STARTERS.length)
    expect(STARTERS.length).toBeGreaterThanOrEqual(3)
  })
})
