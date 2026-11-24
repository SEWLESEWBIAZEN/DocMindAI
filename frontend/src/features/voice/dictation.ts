/**
 * Speech to text for asking questions by voice (ADR 0011).
 *
 * - "browser": the Web Speech API. Some browsers (e.g. Chrome) send audio to
 *   their vendor's recognition service; DocuMind never receives it.
 * - "server": records with MediaRecorder and sends the clip to DocuMind's
 *   transcription endpoint (workspace policy "enabled"); nothing is stored.
 */
import { apiClient } from '../../lib/api/client'
import type { Schemas } from '../../lib/api/types'

export const MAX_RECORDING_MS = 60_000

interface RecognitionResult {
  isFinal: boolean
  0: { transcript: string }
}

interface RecognitionEvent {
  resultIndex: number
  results: ArrayLike<RecognitionResult>
}

interface Recognition {
  lang: string
  interimResults: boolean
  continuous: boolean
  maxAlternatives: number
  onresult: ((event: RecognitionEvent) => void) | null
  onerror: ((event: { error: string }) => void) | null
  onend: (() => void) | null
  start(): void
  stop(): void
  abort(): void
}

type RecognitionCtor = new () => Recognition

function recognitionCtor(): RecognitionCtor | null {
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor }
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null
}

export function browserDictationAvailable(): boolean {
  return typeof window !== 'undefined' && recognitionCtor() !== null
}

export function serverDictationAvailable(): boolean {
  return typeof window !== 'undefined' && 'MediaRecorder' in window && Boolean(navigator.mediaDevices?.getUserMedia)
}

export interface Dictation {
  /** Finish listening and get the transcript. */
  stop(): Promise<string>
  /** Discard. */
  cancel(): void
  /** Resolves with the transcript when listening ends on its own (silence, time limit). */
  done: Promise<string>
}

export interface DictationOptions {
  engine: 'browser' | 'server'
  workspaceId: string
  language?: string
  onInterim?: (text: string) => void
}

export function friendlyDictationError(error: unknown): string {
  const name = error instanceof Error ? error.name || error.message : String(error)
  if (/not-allowed|NotAllowedError|service-not-allowed|SecurityError/i.test(name)) {
    return 'Microphone access was blocked. Allow the microphone for this site and try again.'
  }
  if (/no-speech/i.test(name)) return "I didn't hear anything. Try again and speak after the tone."
  if (/audio-capture|NotFoundError/i.test(name)) return 'No microphone was found.'
  if (/network/i.test(name)) return "The browser's speech service couldn't be reached."
  return error instanceof Error && error.message ? error.message : 'Voice input failed.'
}

function browserDictation({ language, onInterim }: DictationOptions): Dictation {
  const Ctor = recognitionCtor()
  if (!Ctor) throw new Error('Speech recognition is not available in this browser.')
  const recognition = new Ctor()
  recognition.lang = language || navigator.language || 'en-US'
  recognition.interimResults = true
  recognition.continuous = false
  recognition.maxAlternatives = 1
  let finalText = ''
  let interim = ''
  let cancelled = false
  const done = new Promise<string>((resolve, reject) => {
    recognition.onresult = (event) => {
      interim = ''
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i]
        if (!result) continue
        if (result.isFinal) finalText += result[0].transcript
        else interim += result[0].transcript
      }
      onInterim?.(`${finalText}${interim}`.trim())
    }
    recognition.onerror = (event) => {
      if (event.error === 'aborted' || cancelled) resolve('')
      else if (event.error === 'no-speech') resolve('')
      else reject(Object.assign(new Error(event.error), { name: event.error }))
    }
    recognition.onend = () => resolve(`${finalText}${interim}`.trim())
  })
  recognition.start()
  const timer = setTimeout(() => recognition.stop(), MAX_RECORDING_MS)
  void done.finally(() => clearTimeout(timer)).catch(() => undefined)
  return {
    done,
    stop: () => {
      recognition.stop()
      return done
    },
    cancel: () => {
      cancelled = true
      recognition.abort()
    },
  }
}

function pickMimeType(): string {
  for (const type of ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']) {
    if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported?.(type)) return type
  }
  return ''
}

async function serverDictation({ workspaceId, language }: DictationOptions): Promise<Dictation> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
  const mimeType = pickMimeType()
  const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
  const chunks: Blob[] = []
  let cancelled = false
  recorder.ondataavailable = (event) => {
    if (event.data.size > 0) chunks.push(event.data)
  }
  const stopped = new Promise<void>((resolve) => {
    recorder.onstop = () => {
      for (const track of stream.getTracks()) track.stop()
      resolve()
    }
  })
  recorder.start()
  const timer = setTimeout(() => recorder.state !== 'inactive' && recorder.stop(), MAX_RECORDING_MS)
  const done = stopped.then(async () => {
    clearTimeout(timer)
    if (cancelled || chunks.length === 0) return ''
    const type = (recorder.mimeType || mimeType || 'audio/webm').split(';')[0] ?? 'audio/webm'
    const form = new FormData()
    form.append('audio', new Blob(chunks, { type }), `question.${type.includes('mp4') ? 'm4a' : type.split('/')[1]}`)
    if (language) form.append('language', language)
    const { data } = await apiClient.post<Schemas['TranscriptResponse']>(`/workspaces/${workspaceId}/voice/transcribe`, form, {
      timeout: 90_000,
    })
    return data.text.trim()
  })
  return {
    done,
    stop: () => {
      if (recorder.state !== 'inactive') recorder.stop()
      return done
    },
    cancel: () => {
      cancelled = true
      if (recorder.state !== 'inactive') recorder.stop()
    },
  }
}

export async function startDictation(options: DictationOptions): Promise<Dictation> {
  return options.engine === 'server' ? serverDictation(options) : browserDictation(options)
}
