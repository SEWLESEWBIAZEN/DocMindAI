import { describe, expect, it } from 'vitest'

import { can } from '../../lib/workspace/permissions'
import { CAPABILITY_LABELS, formatQuota, quotaPercent, ROUTE_LABELS } from './api'

describe('quota formatting', () => {
  it('formats storage, audio and counts', () => {
    expect(formatQuota('storage_bytes', 1024 ** 3)).toBe('1.0 GB')
    expect(formatQuota('storage_bytes', 500)).toBe('500 B')
    expect(formatQuota('audio_seconds', 3600)).toBe('60 min')
    expect(formatQuota('audio_seconds', 90)).toBe('1.5 min')
    expect(formatQuota('chat_tokens', 2_000_000)).toBe(new Intl.NumberFormat().format(2_000_000))
  })

  it('computes a capped percentage, none for unlimited', () => {
    expect(quotaPercent({ limit: 200, used: 50 })).toBe(25)
    expect(quotaPercent({ limit: 10, used: 25 })).toBe(100)
    expect(quotaPercent({ limit: 0, used: 0 })).toBe(100)
    expect(quotaPercent({ limit: null, used: 999 })).toBeNull()
  })
})

describe('labels', () => {
  it('names every capability and route the API returns', () => {
    for (const cap of ['embedding', 'chat', 'vision', 'speech_to_text', 'text_to_speech']) {
      expect(CAPABILITY_LABELS[cap]).toBeTruthy()
    }
    for (const route of ['workspace_key', 'platform', 'provider_key_required', 'provider_key_invalid', 'capability_unavailable']) {
      expect(ROUTE_LABELS[route]).toBeTruthy()
    }
  })
})

describe('permission mirror', () => {
  it('matches the backend for the new permissions', () => {
    expect(can('owner', 'ai_providers.manage')).toBe(true)
    expect(can('admin', 'ai_providers.manage')).toBe(true)
    expect(can('member', 'ai_providers.manage')).toBe(false)
    expect(can('owner', 'infrastructure.manage')).toBe(true)
    expect(can('admin', 'infrastructure.manage')).toBe(false)
  })
})
