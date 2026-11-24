import { describe, expect, it } from 'vitest'

import { parseSseChunk, readSse } from './sse'

describe('parseSseChunk', () => {
  it('parses complete events and keeps the partial remainder', () => {
    const { events, rest } = parseSseChunk('event: answer.delta\ndata: {"text":"Hi"}\n\nevent: answer.de')
    expect(events).toEqual([{ event: 'answer.delta', data: '{"text":"Hi"}' }])
    expect(rest).toBe('event: answer.de')
  })

  it('skips keep-alive comments and joins multi-line data', () => {
    const { events } = parseSseChunk(': keep-alive\n\ndata: a\ndata: b\n\n')
    expect(events).toEqual([{ event: 'message', data: 'a\nb' }])
  })

  it('accepts CRLF line endings', () => {
    expect(parseSseChunk('event: x\r\ndata: 1\r\n\r\n').events).toEqual([{ event: 'x', data: '1' }])
  })
})

describe('readSse', () => {
  it('reassembles events split across network chunks', async () => {
    const chunks = ['event: a\nda', 'ta: {"n":1}\n', '\nevent: b\ndata: 2\n\n']
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk))
        controller.close()
      },
    })
    const events = []
    for await (const event of readSse(new Response(body))) events.push(event)
    expect(events).toEqual([
      { event: 'a', data: '{"n":1}' },
      { event: 'b', data: '2' },
    ])
  })
})
