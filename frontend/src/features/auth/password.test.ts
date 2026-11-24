import { describe, expect, it } from 'vitest'

import { passwordScore, validatePassword } from './password'
import { safeNext } from './routeGuards'

describe('password rules', () => {
  it('mirrors the server policy (10–128 characters)', () => {
    expect(validatePassword('short')).toMatch(/at least 10/)
    expect(validatePassword('x'.repeat(129))).toMatch(/at most 128/)
    expect(validatePassword('a long passphrase')).toBeNull()
  })

  it('scores variety and length', () => {
    expect(passwordScore('')).toBe(0)
    expect(passwordScore('abc')).toBe(1)
    expect(passwordScore('Correct-Horse-Battery-7')).toBe(4)
  })
})

describe('safeNext', () => {
  it.each([
    ['/w/1/documents', '/w/1/documents'],
    [null, '/app'],
    ['https://evil.test', '/app'],
    ['//evil.test', '/app'],
    ['/\\evil.test', '/app'],
  ])('%s → %s', (input, expected) => {
    expect(safeNext(input)).toBe(expected)
  })
})
