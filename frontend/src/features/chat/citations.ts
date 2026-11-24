/**
 * Turn the answer's [n] markers into Markdown links (`[n](#cite-n)`), so the
 * renderer can show them as citation buttons. Handles grouped markers like
 * [1, 3] and skips fenced and inline code.
 */
const MARKER = /\[(\d+(?:\s*[,;]\s*\d+)*)\](?!\()/g

export function linkCitations(text: string, valid: ReadonlySet<number>): string {
  const parts = text.split(/(```[\s\S]*?```|`[^`\n]*`)/g)
  return parts
    .map((part, index) => {
      if (index % 2 === 1) return part // code
      return part.replace(MARKER, (whole, group: string) => {
        const numbers = group.split(/\s*[,;]\s*/).map(Number)
        if (!numbers.every((n) => valid.has(n))) return whole
        return numbers.map((n) => `[${n}](#cite-${n})`).join('')
      })
    })
    .join('')
}

/** Where a citation's "open source" link goes: the viewer, scrolled to the page, excerpt highlighted. */
export function sourceHref(
  base: string,
  citation: { document_id: string | null; page_start: number | null; excerpt: string },
): string | null {
  if (!citation.document_id) return null
  const params = new URLSearchParams()
  if (citation.page_start) params.set('page', String(citation.page_start))
  if (citation.excerpt) params.set('q', citation.excerpt.slice(0, 300))
  return `${base}/documents/${citation.document_id}?${params.toString()}`
}
