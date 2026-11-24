import { describe, expect, it } from 'vitest'

import { findExcerpt } from './highlight'

const PAGE = 'Renewal\n\nNotice of non-renewal must be\ngiven ninety days before expiry.  Fees apply.'

describe('findExcerpt', () => {
  it('matches across line breaks and case', () => {
    const range = findExcerpt(PAGE, 'notice of non-renewal must be given ninety days before expiry.')
    expect(range).not.toBeNull()
    const [start, end] = range ?? [0, 0]
    expect(PAGE.slice(start, end)).toBe('Notice of non-renewal must be\ngiven ninety days before expiry.')
  })

  it('falls back to the leading words of a trimmed excerpt', () => {
    const range = findExcerpt(PAGE, 'Notice of non-renewal must be given ninety days before the end of term…')
    expect(range).not.toBeNull()
  })

  it('escapes regex characters and returns null when absent', () => {
    expect(findExcerpt('Cost (USD) is $5.', 'Cost (USD) is $5.')).toEqual([0, 17])
    expect(findExcerpt(PAGE, 'parking policy')).toBeNull()
    expect(findExcerpt(PAGE, '')).toBeNull()
  })
})
