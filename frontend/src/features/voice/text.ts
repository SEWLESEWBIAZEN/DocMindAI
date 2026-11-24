import type { ExtractedPage } from '../../lib/api/types'

/** Turning answers and pages into speakable sentences (ADR 0011). */

const MARKERS = /\s*\[(\d+(?:\s*[,;]\s*\d+)*)\]/g
const TRAILING_MARKERS = /([.!?…]["'”’)]*)((?:\s*\[\d+(?:\s*[,;]\s*\d+)*\])+)/g

export interface SpeakableCitation {
  number: number
  spoken_label: string
}

const ABBREVIATIONS = new Set([
  'e.g', 'i.e', 'etc', 'vs', 'no', 'nos', 'p', 'pp', 'fig', 'figs', 'dr', 'mr', 'mrs', 'ms', 'prof',
  'inc', 'ltd', 'co', 'corp', 'jr', 'sr', 'st', 'approx', 'cf', 'al', 'art', 'sec', 'para', 'vol',
])

/**
 * Split into sentences. Only punctuation followed by whitespace ends a
 * sentence (so "4.2" stays whole), and abbreviations or initials before a
 * period ("e.g.", "p. 14", "J. Smith") don't.
 */
export function splitSentences(text: string): string[] {
  const clean = text.replace(/\s+/g, ' ').trim()
  if (!clean) return []
  const sentences: string[] = []
  const terminator = /[.!?…]+["'”’)\]]*(?=\s|$)/g
  let start = 0
  for (let match = terminator.exec(clean); match; match = terminator.exec(clean)) {
    if (match[0].startsWith('.') && match[0].length === 1) {
      const word = (/(\S+)$/.exec(clean.slice(start, match.index))?.[1] ?? '').replace(/^[("'“‘]+/, '')
      if (ABBREVIATIONS.has(word.toLowerCase()) || /^[A-Za-z]$/.test(word) || /^(?:[A-Za-z]\.)+[A-Za-z]$/.test(word)) {
        continue
      }
    }
    const end = match.index + match[0].length
    const sentence = clean.slice(start, end).trim()
    if (sentence) sentences.push(sentence)
    start = end
  }
  const rest = clean.slice(start).trim()
  if (rest) sentences.push(rest)
  return sentences
}

/** Lines first (headings and list items stand alone), then sentences. */
export function splitLines(text: string): string[] {
  return text.split(/\n+/).flatMap(splitSentences)
}

/** Strip Markdown syntax that shouldn't be read out. */
export function plainText(markdown: string): string {
  return markdown
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\((?!#cite)[^)]*\)/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/(\*\*|__|\*|_|~~)(\S[\s\S]*?\S|\S)\1/g, '$2')
    .replace(/\|/g, ' ')
}

/**
 * Sentences for reading an answer aloud. Citation markers become spoken
 * sources ("according to Vendor Policy, page 3") or are dropped.
 */
export function answerToSpeech(
  content: string,
  citations: readonly SpeakableCitation[],
  { speakCitations }: { speakCitations: boolean },
): string[] {
  const labels = new Map(citations.map((c) => [c.number, c.spoken_label]))
  // "Statement. [1]" → "Statement [1]." so the marker stays with its sentence.
  const text = plainText(content).replace(TRAILING_MARKERS, '$2$1')
  return splitLines(text).map((sentence) => {
    const cited: string[] = []
    const bare = sentence.replace(MARKERS, (_, group: string) => {
      for (const n of group.split(/\s*[,;]\s*/).map(Number)) {
        const label = labels.get(n)
        if (label && !cited.includes(label)) cited.push(label)
      }
      return ''
    })
    if (!speakCitations || cited.length === 0) return bare.trim()
    return `${bare.trim()} (Source: ${cited.join('; ')}.)`
  }).filter(Boolean)
}

/** Sources read after a streamed answer finishes. */
export function sourcesSummary(citations: readonly SpeakableCitation[]): string | null {
  if (!citations.length) return null
  const list = citations.map((c) => `${c.number}: ${c.spoken_label}`).join('. ')
  return `Sources. ${list}.`
}

/** Strip citation markers from streamed text (citations aren't known yet). */
export function withoutMarkers(text: string): string {
  return plainText(text).replace(MARKERS, '')
}

/**
 * Complete sentences in `text` after `offset`, for reading a streamed
 * answer as it arrives. Returns the sentences and the new offset.
 */
export function completedSentences(text: string, offset: number): { sentences: string[]; offset: number } {
  const rest = text.slice(offset)
  const match = rest.match(/^[\s\S]*[.!?…]["'”’)\]]*(?:\s*\[\d+(?:\s*[,;]\s*\d+)*\])*\s+(?=\S)/)
  if (!match) return { sentences: [], offset }
  const chunk = match[0]
  return { sentences: splitSentences(withoutMarkers(chunk)), offset: offset + chunk.length }
}

// --- document reading -------------------------------------------------------

export function pageName(page: ExtractedPage): string {
  return page.page_number !== null ? `page ${page.page_number}` : 'the text'
}

/** Sentences for one page: where we are, then the page's text. */
export function pageToSpeech(page: ExtractedPage): string[] {
  const where = page.page_number !== null ? `Page ${page.page_number}.` : ''
  return [where, ...splitLines(page.text)].filter(Boolean)
}

/** Index of the next page that starts a new section, or null. */
export function nextSectionIndex(pages: ExtractedPage[], from: number): number | null {
  const current = pages[from]?.headings[0]
  for (let i = from + 1; i < pages.length; i += 1) {
    const heading = pages[i]?.headings[0]
    if (heading && heading !== current) return i
  }
  return null
}

