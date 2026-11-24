import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useState } from 'react'

import { Alert, Badge, Button, Field, Select, useToast } from '../../components/ui'
import { apiClient } from '../../lib/api/client'
import { errorMessage } from '../../lib/api/errors'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { browserDictationAvailable } from './dictation'
import { MAX_RATE, MIN_RATE, setPreferences } from './settings'
import { SHORTCUTS } from './shortcuts'
import { BrowserEngine } from './speaker'
import { useVoice, voiceKey, type VoiceSettings } from './VoiceContext'

const LANGUAGES: [string, string][] = [
  ['', 'Browser default'],
  ['en-US', 'English (US)'],
  ['en-GB', 'English (UK)'],
  ['de-DE', 'German'],
  ['fr-FR', 'French'],
  ['es-ES', 'Spanish'],
  ['it-IT', 'Italian'],
  ['nl-NL', 'Dutch'],
  ['pt-BR', 'Portuguese (Brazil)'],
  ['am-ET', 'Amharic'],
]

function useBrowserVoices(): SpeechSynthesisVoice[] {
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>(() =>
    BrowserEngine.available() ? window.speechSynthesis.getVoices() : [],
  )
  useEffect(() => {
    if (!BrowserEngine.available()) return
    const update = () => setVoices(window.speechSynthesis.getVoices())
    window.speechSynthesis.addEventListener('voiceschanged', update)
    return () => window.speechSynthesis.removeEventListener('voiceschanged', update)
  }, [])
  return voices
}

export function ShortcutList() {
  return (
    <dl className="meta-list shortcut-list">
      {SHORTCUTS.map(([keys, action]) => (
        <div key={keys} style={{ display: 'contents' }}>
          <dt>
            <kbd>{keys}</kbd>
          </dt>
          <dd>{action}</dd>
        </div>
      ))}
    </dl>
  )
}

/** Personal voice and reading preferences (Profile). */
export function VoiceSettingsPanel() {
  const { prefs, settings, active, ttsEngine, sttEngine, speak } = useVoice()
  const voices = useBrowserVoices()
  const rateId = useId()
  const policy = settings?.policy
  const engineHint =
    policy === 'disabled'
      ? 'Voice is turned off in this workspace by an admin.'
      : policy === 'browser'
        ? "This workspace allows only your browser's speech features."
        : settings?.server_speech
          ? 'DocuMind voice is available here. Audio is sent to the speech provider and never stored.'
          : "DocuMind voice isn't configured on this server; your browser's speech is used."

  return (
    <section className="panel" aria-labelledby="voice-heading" id="voice">
      <header className="panel__header">
        <div>
          <h2 id="voice-heading">Voice and reading</h2>
          <p className="panel__description">
            Ask by voice, hear answers with their sources, and listen to documents. Off by default, so it never talks
            over your screen reader.
          </p>
        </div>
        <Badge tone={active ? 'success' : 'neutral'}>{active ? 'On' : 'Off'}</Badge>
      </header>
      <div className="panel__body stack">
        <label className="checkbox">
          <input type="checkbox" checked={prefs.enabled} onChange={(e) => setPreferences({ enabled: e.target.checked })} />
          <span>
            <strong>Use voice features</strong>
            <span className="field__hint" style={{ display: 'block' }}>
              Adds Listen and microphone buttons. Nothing plays until you press one.
            </span>
          </span>
        </label>
        <p className="field__hint">{engineHint}</p>

        {prefs.enabled && policy !== 'disabled' && (
          <>
            <div className="grid-2">
              <Field label="Voice engine">
                <Select value={prefs.engine} onChange={(e) => setPreferences({ engine: e.target.value as typeof prefs.engine })}>
                  <option value="auto">Automatic</option>
                  <option value="server" disabled={!settings?.server_speech}>
                    DocuMind voice{settings?.server_speech ? '' : ' (not available here)'}
                  </option>
                  <option value="browser" disabled={!BrowserEngine.available()}>
                    Browser voice{BrowserEngine.available() ? '' : ' (not supported)'}
                  </option>
                </Select>
              </Field>
              {ttsEngine === 'server' ? (
                <Field label="DocuMind voice">
                  <Select value={prefs.serverVoice} onChange={(e) => setPreferences({ serverVoice: e.target.value })}>
                    {(settings?.voices ?? []).map((v) => (
                      <option key={v} value={v}>
                        {v[0]?.toUpperCase()}
                        {v.slice(1)}
                      </option>
                    ))}
                  </Select>
                </Field>
              ) : (
                <Field label="Browser voice" hint={voices.length ? undefined : 'Your browser reports no voices yet.'}>
                  <Select value={prefs.browserVoice} onChange={(e) => setPreferences({ browserVoice: e.target.value })}>
                    <option value="">System default</option>
                    {voices.map((v) => (
                      <option key={`${v.name}-${v.lang}`} value={v.name}>
                        {v.name} ({v.lang}){v.localService ? '' : ' · online'}
                      </option>
                    ))}
                  </Select>
                </Field>
              )}
            </div>
            <div className="field">
              <label htmlFor={rateId} className="field__label">
                Reading speed: {prefs.rate.toFixed(1)}×
              </label>
              <input
                id={rateId}
                type="range"
                min={MIN_RATE}
                max={MAX_RATE}
                step={0.1}
                value={prefs.rate}
                onChange={(e) => setPreferences({ rate: Number(e.target.value) })}
                aria-valuetext={`${prefs.rate.toFixed(1)} times`}
              />
            </div>
            <div className="stack stack--sm">
              <label className="checkbox">
                <input type="checkbox" checked={prefs.speakCitations} onChange={(e) => setPreferences({ speakCitations: e.target.checked })} />
                <span>Say which document and page each statement comes from</span>
              </label>
              <label className="checkbox">
                <input type="checkbox" checked={prefs.autoRead} onChange={(e) => setPreferences({ autoRead: e.target.checked })} />
                <span>Read answers aloud as they arrive, after I ask a question</span>
              </label>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={prefs.sendAfterDictation}
                  onChange={(e) => setPreferences({ sendAfterDictation: e.target.checked })}
                />
                <span>Send spoken questions right away (otherwise you can review them first)</span>
              </label>
            </div>
            <div className="grid-2">
              <Field
                label="Voice input language"
                hint={
                  sttEngine
                    ? sttEngine === 'browser' && browserDictationAvailable()
                      ? "Uses your browser's speech recognition. Some browsers process it with their vendor's online service."
                      : 'Recorded and transcribed by DocuMind; recordings are not stored.'
                    : 'Voice input is not supported in this browser.'
                }
              >
                <Select value={prefs.language} onChange={(e) => setPreferences({ language: e.target.value })}>
                  {LANGUAGES.map(([code, label]) => (
                    <option key={code} value={code}>
                      {label}
                    </option>
                  ))}
                </Select>
              </Field>
              <div className="field" style={{ alignContent: 'end' }}>
                <Button
                  icon="volume"
                  disabled={!ttsEngine}
                  onClick={() =>
                    speak(
                      [
                        'This is how answers will sound.',
                        'Notice must be given ninety days before expiry. (Source: Master Services Agreement, page 14.)',
                      ],
                      { label: 'voice test' },
                    )
                  }
                >
                  Test voice
                </Button>
              </div>
            </div>
          </>
        )}

        <div>
          <h3 style={{ marginBottom: 8 }}>Keyboard shortcuts</h3>
          <ShortcutList />
        </div>
      </div>
    </section>
  )
}

const POLICIES: { value: VoiceSettings['policy']; title: string; description: string }[] = [
  {
    value: 'enabled',
    title: 'DocuMind voice and browser voice',
    description:
      'Members can use natural voices and transcription from DocuMind. Audio is sent to the speech provider and never stored.',
  },
  {
    value: 'browser',
    title: 'Browser voice only',
    description:
      "DocuMind never receives audio. Members use their browser's built-in speech (some browsers process recognition online).",
  },
  { value: 'disabled', title: 'Off', description: 'Voice features are hidden for everyone in this workspace.' },
]

/** Workspace voice policy (Settings, admins). */
export function WorkspaceVoicePolicy() {
  const { workspace, can } = useWorkspace()
  const { settings } = useVoice()
  const queryClient = useQueryClient()
  const toast = useToast()
  const [chosen, setChosen] = useState<VoiceSettings['policy'] | null>(null)
  const save = useMutation({
    onSettled: () => setChosen(null),
    mutationFn: async (policy: VoiceSettings['policy']) =>
      (await apiClient.put<VoiceSettings>(`/workspaces/${workspace.id}/voice`, { policy })).data,
    onSuccess: (data) => {
      queryClient.setQueryData(voiceKey(workspace.id), data)
      toast.success('Voice policy updated.')
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  if (!settings) return null
  const editable = can('workspace.update')
  // Show the choice immediately while it saves.
  const current = chosen ?? settings.policy
  return (
    <section className="panel" aria-labelledby="voice-policy-heading">
      <header className="panel__header">
        <div>
          <h2 id="voice-policy-heading">Voice</h2>
          <p className="panel__description">Choose which voice features members of {workspace.name} may use.</p>
        </div>
      </header>
      <div className="panel__body stack">
        <fieldset className="radio-group" disabled={!editable}>
          <legend className="sr-only">Voice policy</legend>
          {POLICIES.map((p) => (
            <label key={p.value} className="radio-card">
              <input
                type="radio"
                name="voice-policy"
                value={p.value}
                checked={current === p.value}
                onChange={() => {
                  setChosen(p.value)
                  save.mutate(p.value)
                }}
              />
              <span>
                <strong>{p.title}</strong>
                <span className="field__hint" style={{ display: 'block' }}>
                  {p.description}
                </span>
              </span>
            </label>
          ))}
        </fieldset>
        {!settings.provider_configured && settings.policy === 'enabled' && (
          <Alert tone="info">DocuMind voice isn't configured on this server yet, so members use browser voice.</Alert>
        )}
        {!editable && <p className="field__hint">Only admins can change this.</p>}
      </div>
    </section>
  )
}
