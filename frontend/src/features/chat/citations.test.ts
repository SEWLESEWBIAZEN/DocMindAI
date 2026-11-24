import { describe, expect, it } from 'vitest'

import { linkCitations, sourceHref } from './citations'

const VALID = new Set([1, 2, 3])

describe('linkCitations', () => {
  it('links single and grouped markers', () => {
    expect(linkCitations('A [1]. B [2, 3].', VALID)).toBe('A [1](#cite-1). B [2](#cite-2)[3](#cite-3).')
  })

  it('leaves unknown numbers and existing links alone', () => {
    expect(linkCitations('X [9]. [link](https://x.test) [1]', VALID)).toBe('X [9]. [link](https://x.test) [1](#cite-1)')
  })

  it('does not touch code', () => {
    expect(linkCitations('`arr[1]` and\n```\nx[2]\n```\n[3]', VALID)).toBe('`arr[1]` and\n```\nx[2]\n```\n[3](#cite-3)')
  })
})

describe('sourceHref', () => {
  it('links to the page with the excerpt to highlight', () => {
    const href = sourceHref('/w/ws1', { document_id: 'd1', page_start: 14, excerpt: 'Notice is 90 days.' })
    expect(href).toBe('/w/ws1/documents/d1?page=14&q=Notice+is+90+days.')
  })

  it('returns null when the document is gone', () => {
    expect(sourceHref('/w/ws1', { document_id: null, page_start: 1, excerpt: 'x' })).toBeNull()
  })
})
