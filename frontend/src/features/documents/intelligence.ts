import type { Schemas } from '../../lib/api/types'

type Citation = Schemas['CitationResponse']

/** Questions that work on almost any document; shown before the first question. */
export const STARTERS = [
  'Summarize this document in five bullet points.',
  'What are the key dates, deadlines and amounts?',
  'What obligations, risks or recommendations does it set out?',
] as const

/** "p. 12 · Termination" — where in the document a citation points. */
export function citationPlace(c: Pick<Citation, 'page_start' | 'page_end' | 'heading_path'>): string {
  const pages =
    c.page_start === null ? '' : c.page_end && c.page_end !== c.page_start ? `pp. ${c.page_start}–${c.page_end}` : `p. ${c.page_start}`
  const section = c.heading_path.at(-1) ?? ''
  return [pages, section].filter(Boolean).join(' · ') || 'Passage'
}
