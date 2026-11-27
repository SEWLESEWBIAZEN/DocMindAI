import { Link } from 'react-router'

import { Icon } from '../../components/ui'
import type { Citation } from '../../lib/api/types'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { sourceHref } from './citations'

export function CitationCard({ citation, active, onPreview }: { citation: Citation; active: boolean; onPreview?: () => void }) {
  const { base } = useWorkspace()
  const href = citation.document_available ? sourceHref(base, citation) : null
  return (
    <div className={`citation-card${active ? ' citation-card--active' : ''}`} data-cite={citation.number} id={`cite-card-${citation.number}`}>
      <div className="citation-card__head">
        <span className="citation-card__num" aria-hidden="true">
          {citation.number}
        </span>
        <span className="citation-card__doc" title={citation.label}>
          <span className="sr-only">Source {citation.number}: </span>
          {citation.label}
        </span>
      </div>
      <p className="citation-card__excerpt">“{citation.excerpt}”</p>
      {href ? (
        <span className="row citation-card__links">
          {onPreview && (
            <button type="button" className="link-button citation-card__link" aria-label={`Preview ${citation.spoken_label}`} onClick={onPreview}>
              <Icon name="eye" size={13} /> Preview
            </button>
          )}
          <Link to={href} className="citation-card__link" aria-label={`Open ${citation.spoken_label}`}>
            Open source <Icon name="arrowRight" size={13} />
          </Link>
        </span>
      ) : (
        <span className="muted" style={{ fontSize: '0.8rem' }}>
          The document is no longer available.
        </span>
      )}
    </div>
  )
}

