import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'

import { Button, ErrorState, Icon, Loading } from '../../components/ui'
import type { Citation } from '../../lib/api/types'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { allPages, documentKeys } from '../documents/api'
import { findExcerpt } from '../documents/highlight'
import { sourceHref } from './citations'

/**
 * The cited page beside the answer, with the cited passage highlighted. Moves page by
 * page through the document; "Open document" goes to the full viewer at the same place.
 */
export function SourcePreview({ citation, onClose }: { citation: Citation; onClose: () => void }) {
  const { workspace, base } = useWorkspace()
  const documentId = citation.document_id ?? ''
  const pages = useQuery({
    queryKey: documentKeys.pages(workspace.id, documentId),
    queryFn: () => allPages(workspace.id, documentId),
    enabled: Boolean(documentId) && citation.document_available,
  })
  const list = pages.data ?? []
  const citedIndex = Math.max(
    0,
    list.findIndex((p) => (p.page_number ?? p.position + 1) === (citation.page_start ?? 1)),
  )
  // Follow the cited page until the reader moves; a new citation resets the position.
  const [moved, setMoved] = useState<{ key: string; index: number } | null>(null)
  const key = `${documentId}:${citation.number}:${citation.page_start}`
  const index = moved?.key === key ? moved.index : citedIndex
  const page = list[index]
  const numbered = list.some((p) => p.page_number !== null)
  const range = page && index === citedIndex ? findExcerpt(page.text, citation.excerpt) : null
  const href = citation.document_available ? sourceHref(base, citation) : null

  return (
    <section className="source-preview" aria-label="Source preview">
      <header className="source-preview__header">
        <h2>Source preview</h2>
        <Button variant="ghost" size="sm" icon="x" label="Close source preview" onClick={onClose} />
      </header>
      <div className="source-preview__doc">
        <Icon name="file" size={16} />
        <div className="stack" style={{ gap: 2, minWidth: 0 }}>
          <strong className="truncate">{citation.document_name ?? citation.label}</strong>
          <span className="muted">
            {numbered && list.length > 0 ? `Page ${page?.page_number ?? index + 1} of ${list.length}` : citation.label}
          </span>
        </div>
      </div>
      <div className="source-preview__page" tabIndex={0} aria-label="Page text">
        {!citation.document_available ? (
          <p className="muted">This document is no longer available. The quoted passage was:</p>
        ) : pages.isLoading ? (
          <Loading label="Loading page" lines={8} />
        ) : pages.error ? (
          <ErrorState error={pages.error} onRetry={() => void pages.refetch()} />
        ) : page ? (
          <div className="source-preview__sheet">
            {page.headings[0] && <p className="source-preview__heading">{page.headings[0]}</p>}
            <p className="source-preview__text">
              {range ? (
                <>
                  {page.text.slice(0, range[0])}
                  <mark className="cite-highlight">{page.text.slice(range[0], range[1])}</mark>
                  {page.text.slice(range[1])}
                </>
              ) : (
                page.text
              )}
            </p>
          </div>
        ) : null}
        {!citation.document_available && <blockquote className="source-preview__quote">{citation.excerpt}</blockquote>}
      </div>
      <footer className="source-preview__footer">
        {list.length > 1 && (
          <div className="row" role="group" aria-label="Pages">
            <Button variant="ghost" size="sm" icon="arrowLeft" label="Previous page" disabled={index <= 0} onClick={() => setMoved({ key, index: index - 1 })} />
            <span className="source-preview__count">
              {index + 1} / {list.length}
            </span>
            <Button
              variant="ghost"
              size="sm"
              icon="arrowRight"
              label="Next page"
              disabled={index >= list.length - 1}
              onClick={() => setMoved({ key, index: index + 1 })}
            />
          </div>
        )}
        {href && (
          <Link to={href} className="btn btn--secondary btn--sm">
            Open document <Icon name="arrowRight" size={14} />
          </Link>
        )}
      </footer>
    </section>
  )
}
