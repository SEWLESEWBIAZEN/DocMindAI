import { memo } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { linkCitations } from './citations'

const SAFE_PROTOCOL = /^(https?:|mailto:)/i

/**
 * Renders model output as Markdown. Raw HTML is never rendered (react-markdown
 * escapes it), links are limited to http(s)/mailto and open in a new tab, and
 * [n] markers become citation buttons.
 */
export const AnswerMarkdown = memo(function AnswerMarkdown({
  text,
  citationNumbers,
  activeCitation,
  onCitation,
}: {
  text: string
  citationNumbers: ReadonlySet<number>
  activeCitation?: number | null
  onCitation?: (n: number) => void
}) {
  const components: Components = {
    a({ href, children }) {
      const cite = href?.match(/^#cite-(\d+)$/)
      if (cite) {
        const n = Number(cite[1])
        return (
          <button
            type="button"
            className="cite-ref"
            aria-label={`Source ${n}`}
            aria-pressed={activeCitation === n}
            onClick={() => onCitation?.(n)}
          >
            {n}
          </button>
        )
      }
      if (!href || !SAFE_PROTOCOL.test(href)) return <span>{children}</span>
      return (
        <a href={href} target="_blank" rel="noopener noreferrer nofollow">
          {children}
        </a>
      )
    },
    img({ alt }) {
      // No remote images from model output (tracking, layout); show the alt text.
      return <span>{alt}</span>
    },
  }
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} skipHtml>
        {linkCitations(text, citationNumbers)}
      </ReactMarkdown>
    </div>
  )
})
