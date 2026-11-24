import { useSyncExternalStore } from 'react'

/**
 * Personal voice preferences (ADR 0011). Stored per browser: voice is
 * opt-in, and output never starts unless the user turns it on.
 */
export interface VoicePreferences {
  enabled: boolean
  /** Read answers aloud as they arrive (only after the user asks a question). */
  autoRead: boolean
  speakCitations: boolean
  /** Send a dictated question immediately instead of placing it in the box. */
  sendAfterDictation: boolean
  rate: number
  engine: 'auto' | 'browser' | 'server'
  browserVoice: string
  serverVoice: string
  language: string
}

export const DEFAULT_PREFERENCES: VoicePreferences = {
  enabled: false,
  autoRead: false,
  speakCitations: true,
  sendAfterDictation: false,
  rate: 1,
  engine: 'auto',
  browserVoice: '',
  serverVoice: 'alloy',
  language: '',
}

const KEY = 'documind.voice.v1'
export const MIN_RATE = 0.5
export const MAX_RATE = 2

let current: VoicePreferences = load()
const listeners = new Set<() => void>()

function load(): VoicePreferences {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (raw) return { ...DEFAULT_PREFERENCES, ...(JSON.parse(raw) as Partial<VoicePreferences>) }
  } catch {
    // storage unavailable (private mode, blocked): use defaults
  }
  return DEFAULT_PREFERENCES
}

export function clampRate(rate: number): number {
  return Math.round(Math.min(MAX_RATE, Math.max(MIN_RATE, rate)) * 100) / 100
}

export function getPreferences(): VoicePreferences {
  return current
}

export function setPreferences(change: Partial<VoicePreferences>): void {
  current = { ...current, ...change, rate: clampRate(change.rate ?? current.rate) }
  try {
    window.localStorage.setItem(KEY, JSON.stringify(current))
  } catch {
    // not persisted; still applies for this session
  }
  for (const listener of listeners) listener()
}

export function resetPreferencesForTests(): void {
  current = DEFAULT_PREFERENCES
}

export function useVoicePreferences(): VoicePreferences {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => current,
  )
}
