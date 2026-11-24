import { useQuery } from '@tanstack/react-query'
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react'

import { ANNOUNCE_EVENT, Button, Icon, useToast } from '../../components/ui'
import { apiClient } from '../../lib/api/client'
import type { Schemas } from '../../lib/api/types'
import { browserDictationAvailable, serverDictationAvailable } from './dictation'
import { getPreferences, setPreferences, clampRate, useVoicePreferences, type VoicePreferences } from './settings'
import { BrowserEngine, speaker, type SpeakOptions, type SpeakerState } from './speaker'

export type VoiceSettings = Schemas['VoiceSettingsResponse']

export const voiceKey = (ws: string) => ['ws', ws, 'voice'] as const

interface VoiceContextValue {
  prefs: VoicePreferences
  settings: VoiceSettings | undefined
  /** Voice output and input may be used here (user opted in, workspace allows it). */
  active: boolean
  ttsEngine: 'browser' | 'server' | null
  sttEngine: 'browser' | 'server' | null
  state: SpeakerState
  speak: (sentences: string[], options: Pick<SpeakOptions, 'label' | 'onSentence' | 'onEnd'>) => boolean
  openStream: (label: string) => boolean
  append: (sentences: string[]) => void
  closeStream: () => void
  stop: () => void
  /** The chat page registers what Ctrl+Shift+Space does when nothing is being read. */
  setDictationShortcut: (handler: (() => void) | null) => void
}

const VoiceContext = createContext<VoiceContextValue | null>(null)

export function chooseEngine(
  preference: VoicePreferences['engine'],
  server: boolean,
  browser: boolean,
): 'browser' | 'server' | null {
  if (preference === 'server') return server ? 'server' : browser ? 'browser' : null
  if (preference === 'browser') return browser ? 'browser' : server ? 'server' : null
  return server ? 'server' : browser ? 'browser' : null
}

export function VoiceProvider({ workspaceId, children }: { workspaceId: string; children: ReactNode }) {
  const prefs = useVoicePreferences()
  const toast = useToast()
  const { data: settings } = useQuery({
    queryKey: voiceKey(workspaceId),
    queryFn: async () => (await apiClient.get<VoiceSettings>(`/workspaces/${workspaceId}/voice`)).data,
    staleTime: 60_000,
  })
  const state = useSyncExternalStore(speaker.subscribe, speaker.getState)
  const dictationShortcut = useRef<(() => void) | null>(null)
  const [announcement, setAnnouncement] = useState('')

  const policy = settings?.policy ?? 'browser'
  const serverAllowed = Boolean(settings?.server_speech)
  const active = prefs.enabled && policy !== 'disabled'
  const ttsEngine = active ? chooseEngine(prefs.engine, serverAllowed, BrowserEngine.available()) : null
  const sttEngine = active
    ? chooseEngine(prefs.engine, serverAllowed && serverDictationAvailable(), browserDictationAvailable())
    : null

  // Leaving the workspace (or turning voice off) stops speech immediately.
  useEffect(() => () => speaker.stop(), [workspaceId])
  useEffect(() => {
    if (!active) speaker.stop()
  }, [active])

  useEffect(() => {
    if (state.error) toast.error(`Reading stopped: ${state.error}`)
  }, [state.error, toast])

  const options = useCallback(
    (label: string): SpeakOptions | null => {
      if (!ttsEngine) return null
      const p = getPreferences()
      return {
        label,
        rate: p.rate,
        engine: ttsEngine,
        workspaceId,
        browserVoice: p.browserVoice,
        serverVoice: p.serverVoice,
      }
    },
    [ttsEngine, workspaceId],
  )

  const speak = useCallback<VoiceContextValue['speak']>(
    (sentences, extra) => {
      const base = options(extra.label)
      if (!base || sentences.length === 0) return false
      speaker.play(sentences, { ...base, onSentence: extra.onSentence, onEnd: extra.onEnd })
      setAnnouncement(`Reading ${extra.label}. Press Escape to stop.`)
      return true
    },
    [options],
  )

  const openStream = useCallback(
    (label: string) => {
      const base = options(label)
      if (!base) return false
      speaker.openStream(base)
      return true
    },
    [options],
  )

  const stop = useCallback(() => {
    if (speaker.getState().status !== 'idle') {
      speaker.stop()
      setAnnouncement('Reading stopped.')
    }
  }, [])

  // Global shortcuts (documented in Profile → Voice and the shortcuts dialog).
  useEffect(() => {
    if (!active) return
    const onKey = (event: KeyboardEvent) => {
      const reading = speaker.getState().status !== 'idle'
      if (event.key === 'Escape' && reading && !event.defaultPrevented && !document.querySelector('dialog[open]')) {
        event.preventDefault()
        stop()
      } else if (event.ctrlKey && event.shiftKey && (event.code === 'Space' || event.key === ' ')) {
        event.preventDefault()
        if (reading) {
          speaker.toggle()
          setAnnouncement(speaker.getState().status === 'paused' ? 'Paused.' : 'Resumed.')
        } else {
          dictationShortcut.current?.()
        }
      } else if (event.ctrlKey && event.shiftKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
        event.preventDefault()
        const rate = clampRate(getPreferences().rate + (event.key === 'ArrowUp' ? 0.1 : -0.1))
        setPreferences({ rate })
        speaker.setRate(rate)
        setAnnouncement(`Speed ${rate.toFixed(1)}.`)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [active, stop])

  // Spoken confirmations: dialogs announce themselves; we read them when voice is on.
  useEffect(() => {
    if (!ttsEngine) return
    const onAnnounce = (event: Event) => {
      const text = (event as CustomEvent<{ text: string }>).detail?.text
      const base = options('confirmation')
      if (text && base) speaker.play([text], base)
    }
    window.addEventListener(ANNOUNCE_EVENT, onAnnounce)
    return () => window.removeEventListener(ANNOUNCE_EVENT, onAnnounce)
  }, [ttsEngine, options])

  const value = useMemo<VoiceContextValue>(
    () => ({
      prefs,
      settings,
      active,
      ttsEngine,
      sttEngine,
      state,
      speak,
      openStream,
      append: (sentences) => speaker.append(sentences),
      closeStream: () => speaker.closeStream(),
      stop,
      setDictationShortcut: (handler) => {
        dictationShortcut.current = handler
      },
    }),
    [prefs, settings, active, ttsEngine, sttEngine, state, speak, openStream, stop],
  )

  return (
    <VoiceContext.Provider value={value}>
      {children}
      {active && <ReadingBar state={state} onStop={stop} />}
      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>
    </VoiceContext.Provider>
  )
}

export function useVoice(): VoiceContextValue {
  const value = useContext(VoiceContext)
  if (!value) throw new Error('useVoice must be used inside <VoiceProvider>')
  return value
}

function ReadingBar({ state, onStop }: { state: SpeakerState; onStop: () => void }) {
  if (state.status === 'idle') return null
  const change = (delta: number) => {
    const rate = clampRate(state.rate + delta)
    setPreferences({ rate })
    speaker.setRate(rate)
  }
  return (
    <section className="reading-bar" aria-label="Reading controls">
      <span className="reading-bar__icon" aria-hidden="true">
        <Icon name="headset" size={16} />
      </span>
      <div className="reading-bar__text">
        <strong className="truncate">
          {state.status === 'paused' ? 'Paused' : 'Reading'}: {state.label}
        </strong>
        <span className="muted">
          {state.total > 0 ? `${Math.min(state.index + 1, state.total)} of ${state.total} · ` : ''}
          {state.rate.toFixed(1)}× · {state.engine === 'server' ? 'DocuMind voice' : 'browser voice'}
        </span>
      </div>
      <div className="reading-bar__actions">
        <Button size="sm" variant="ghost" label="Slower" icon="arrowLeft" onClick={() => change(-0.1)} />
        <Button
          size="sm"
          variant="secondary"
          icon={state.status === 'paused' ? 'play' : 'pause'}
          onClick={() => speaker.toggle()}
          aria-keyshortcuts="Control+Shift+Space"
        >
          {state.status === 'paused' ? 'Resume' : 'Pause'}
        </Button>
        <Button size="sm" variant="ghost" label="Faster" icon="arrowRight" onClick={() => change(0.1)} />
        <Button size="sm" variant="ghost" icon="x" onClick={onStop} aria-keyshortcuts="Escape">
          Stop
        </Button>
      </div>
    </section>
  )
}
