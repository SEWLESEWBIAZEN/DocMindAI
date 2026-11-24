/**
 * Locate a citation excerpt inside page text, tolerating whitespace and case
 * differences (excerpts are taken from chunks, which normalise whitespace).
 * Returns [start, end) offsets into `text`, or null.
 */
export function findExcerpt(text: string, excerpt: string): [number, number] | null {
  const words = excerpt
    .replace(/[…]$/, '')
    .split(/\s+/)
    .filter(Boolean)
    .map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  if (words.length === 0) return null
  const attempt = (parts: string[]): [number, number] | null => {
    if (parts.length === 0) return null
    const match = new RegExp(parts.join('\\s+'), 'i').exec(text)
    return match ? [match.index, match.index + match[0].length] : null
  }
  // Whole excerpt, then progressively shorter leading runs (excerpts can be trimmed).
  for (const size of [words.length, 16, 10, 6]) {
    if (size > words.length) continue
    const found = attempt(words.slice(0, size))
    if (found) return found
  }
  return null
}
