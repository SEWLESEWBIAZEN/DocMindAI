/**
 * Reads text aloud one sentence at a time (ADR 0011).
 *
 * Two engines: the browser's speechSynthesis (no audio leaves the device for
 * most voices) and DocuMind's speech endpoint (natural voices; allowed only
 * when the workspace policy is "enabled"). One shared queue so starting new
 * speech always stops the old, and stop is instant.
 */
import { apiClient } from '../../lib/api/client'

export type SpeakerStatus = 'idle' | 'speaking' | 'paused'

export interface SpeakerState {
  status: SpeakerStatus
  label: string
  index: number
  total: number
  sentence: string
  rate: number
  engine: 'browser' | 'server' | null
  error: string | null
}

export interface SpeakOptions {
  label: string
  rate: number
  engine: 'browser' | 'server'
  workspaceId?: string
  browserVoice?: string
  serverVoice?: string
  /** Called as each sentence starts (e.g. to highlight it). */
  onSentence?: (sentence: string, index: number) => void
  /** Called when the queue finishes on its own (not when stopped). */
  onEnd?: () => void
}

interface Engine {
  speak(text: string, rate: number): Promise<void>
  pause(): void
  resume(): void
  cancel(): void
}

class StoppedError extends Error {}

export class BrowserEngine implements Engine {
  private voiceName: string
  private utterance: SpeechSynthesisUtterance | null = null

  constructor(voiceName = '') {
    this.voiceName = voiceName
  }

  static available(): boolean {
    return typeof window !== 'undefined' && 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window
  }

  speak(text: string, rate: number): Promise<void> {
    return new Promise((resolve, reject) => {
      const utterance = new SpeechSynthesisUtterance(text)
      utterance.rate = rate
      const voice = window.speechSynthesis.getVoices().find((v) => v.name === this.voiceName)
      if (voice) utterance.voice = voice
      utterance.onend = () => resolve()
      utterance.onerror = (event) =>
        event.error === 'interrupted' || event.error === 'canceled'
          ? reject(new StoppedError())
          : reject(new Error(`Speech failed (${event.error})`))
      this.utterance = utterance
      window.speechSynthesis.speak(utterance)
    })
  }

  pause() {
    window.speechSynthesis.pause()
  }

  resume() {
    window.speechSynthesis.resume()
  }

  cancel() {
    if (this.utterance) {
      this.utterance.onend = null
      this.utterance.onerror = null
    }
    window.speechSynthesis.cancel()
  }
}

export class ServerEngine implements Engine {
  private audio: HTMLAudioElement | null = null
  private controller = new AbortController()
  private prefetched = new Map<string, Promise<string>>()
  private rejectCurrent: ((error: Error) => void) | null = null
  private workspaceId: string
  private voice: string

  constructor(workspaceId: string, voice: string) {
    this.workspaceId = workspaceId
    this.voice = voice
  }

  private fetchAudio(text: string, rate: number): Promise<string> {
    const key = `${rate}|${text}`
    let pending = this.prefetched.get(key)
    if (!pending) {
      pending = apiClient
        .post<Blob>(
          `/workspaces/${this.workspaceId}/voice/speech`,
          { text, voice: this.voice || undefined, speed: rate },
          { responseType: 'blob', signal: this.controller.signal, timeout: 60_000 },
        )
        .then((response) => URL.createObjectURL(response.data))
      this.prefetched.set(key, pending)
    }
    return pending
  }

  prefetch(text: string, rate: number) {
    void this.fetchAudio(text, rate).catch(() => undefined)
  }

  async speak(text: string, rate: number): Promise<void> {
    const url = await this.fetchAudio(text, rate)
    this.prefetched.delete(`${rate}|${text}`)
    await new Promise<void>((resolve, reject) => {
      const audio = new Audio(url)
      this.audio = audio
      this.rejectCurrent = reject
      audio.onended = () => {
        URL.revokeObjectURL(url)
        resolve()
      }
      audio.onerror = () => {
        URL.revokeObjectURL(url)
        reject(new Error('Audio playback failed'))
      }
      audio.play().catch(reject)
    })
  }

  pause() {
    this.audio?.pause()
  }

  resume() {
    void this.audio?.play()
  }

  cancel() {
    this.controller.abort()
    this.controller = new AbortController()
    if (this.audio) {
      this.audio.onended = null
      this.audio.pause()
    }
    this.rejectCurrent?.(new StoppedError())
    for (const pending of this.prefetched.values()) void pending.then((url) => URL.revokeObjectURL(url)).catch(() => undefined)
    this.prefetched.clear()
  }
}

const IDLE: SpeakerState = {
  status: 'idle',
  label: '',
  index: 0,
  total: 0,
  sentence: '',
  rate: 1,
  engine: null,
  error: null,
}

export class Speaker {
  private state: SpeakerState = IDLE
  private listeners = new Set<() => void>()
  private queue: string[] = []
  private engine: Engine | null = null
  private options: SpeakOptions | null = null
  private run = 0
  private resumeWaiter: (() => void) | null = null
  private open = false
  private moreComing: (() => void) | null = null
  private engineFactory: (options: SpeakOptions) => Engine

  constructor(engineFactory?: (options: SpeakOptions) => Engine) {
    this.engineFactory =
      engineFactory ??
      ((o) => (o.engine === 'server' && o.workspaceId ? new ServerEngine(o.workspaceId, o.serverVoice ?? '') : new BrowserEngine(o.browserVoice)))
  }

  getState = (): SpeakerState => this.state

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  private set(change: Partial<SpeakerState>) {
    this.state = { ...this.state, ...change }
    for (const listener of this.listeners) listener()
  }

  /** Replace whatever is playing with `sentences`. */
  play(sentences: string[], options: SpeakOptions): void {
    this.start(sentences, options, false)
  }

  /** Start a queue that more sentences will be appended to (streamed answers). */
  openStream(options: SpeakOptions): void {
    this.start([], options, true)
  }

  append(sentences: string[]): void {
    if (!this.open && this.state.status === 'idle') return
    this.queue.push(...sentences.filter((s) => s.trim()))
    this.set({ total: this.queue.length })
    this.moreComing?.()
  }

  /** No more sentences will be appended; finish what is queued. */
  closeStream(): void {
    this.open = false
    this.moreComing?.()
  }

  private start(sentences: string[], options: SpeakOptions, open: boolean) {
    this.stop()
    const run = ++this.run
    this.options = options
    this.queue = sentences.filter((s) => s.trim())
    this.open = open
    this.engine = this.engineFactory(options)
    this.set({
      status: 'speaking',
      label: options.label,
      index: 0,
      total: this.queue.length,
      sentence: '',
      rate: options.rate,
      engine: options.engine,
      error: null,
    })
    void this.loop(run)
  }

  private async loop(run: number) {
    let index = 0
    while (run === this.run) {
      if (index >= this.queue.length) {
        if (!this.open) break
        await new Promise<void>((resolve) => {
          this.moreComing = resolve
        })
        this.moreComing = null
        continue
      }
      if (this.state.status === 'paused') {
        await new Promise<void>((resolve) => {
          this.resumeWaiter = resolve
        })
        continue
      }
      const sentence = this.queue[index] ?? ''
      this.set({ index, sentence })
      this.options?.onSentence?.(sentence, index)
      const next = this.queue[index + 1]
      if (next && this.engine instanceof ServerEngine) this.engine.prefetch(next, this.state.rate)
      try {
        await this.engine?.speak(sentence, this.state.rate)
      } catch (error) {
        if (error instanceof StoppedError || run !== this.run) return
        this.set({ status: 'idle', error: error instanceof Error ? error.message : 'Speech failed' })
        return
      }
      index += 1
    }
    if (run === this.run) {
      const onEnd = this.options?.onEnd
      this.set({ ...IDLE, rate: this.state.rate })
      onEnd?.()
    }
  }

  pause(): void {
    if (this.state.status !== 'speaking') return
    this.engine?.pause()
    this.set({ status: 'paused' })
  }

  resume(): void {
    if (this.state.status !== 'paused') return
    this.set({ status: 'speaking' })
    this.engine?.resume()
    this.resumeWaiter?.()
    this.resumeWaiter = null
  }

  toggle(): void {
    if (this.state.status === 'speaking') this.pause()
    else if (this.state.status === 'paused') this.resume()
  }

  /** Applies from the next sentence. */
  setRate(rate: number): void {
    this.set({ rate })
  }

  stop(): void {
    this.run += 1
    this.engine?.cancel()
    this.engine = null
    this.queue = []
    this.open = false
    this.resumeWaiter?.()
    this.resumeWaiter = null
    this.moreComing?.()
    this.moreComing = null
    if (this.state.status !== 'idle') this.set({ ...IDLE, rate: this.state.rate })
  }
}

export const speaker = new Speaker()
