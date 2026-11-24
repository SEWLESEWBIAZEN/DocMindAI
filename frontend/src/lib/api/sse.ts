/**
 * Minimal Server-Sent Events reader over fetch (EventSource can't POST or send
 * headers). Yields `{event, data}` for each event; comment lines (keep-alives)
 * are skipped. Aborting the signal closes the connection, which the backend
 * treats as a cancel.
 */
export interface SseEvent {
  event: string
  data: string
}

export function parseSseChunk(buffer: string): { events: SseEvent[]; rest: string } {
  const events: SseEvent[] = []
  const normalized = buffer.replace(/\r\n?/g, '\n')
  const blocks = normalized.split('\n\n')
  const rest = blocks.pop() ?? ''
  for (const block of blocks) {
    let event = 'message'
    const data: string[] = []
    for (const line of block.split('\n')) {
      if (!line || line.startsWith(':')) continue
      const colon = line.indexOf(':')
      const field = colon === -1 ? line : line.slice(0, colon)
      let value = colon === -1 ? '' : line.slice(colon + 1)
      if (value.startsWith(' ')) value = value.slice(1)
      if (field === 'event') event = value
      else if (field === 'data') data.push(value)
    }
    if (data.length) events.push({ event, data: data.join('\n') })
  }
  return { events, rest }
}

export async function* readSse(response: Response): AsyncGenerator<SseEvent> {
  if (!response.body) return
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += value
      const { events, rest } = parseSseChunk(buffer)
      buffer = rest
      yield* events
    }
    if (buffer.trim()) yield* parseSseChunk(`${buffer}\n\n`).events
  } finally {
    reader.releaseLock()
  }
}
