import { Button } from './Button'

export function Pagination({
  offset,
  limit,
  total,
  onChange,
  label = 'items',
}: {
  offset: number
  limit: number
  total: number
  onChange: (offset: number) => void
  label?: string
}) {
  if (total <= limit) return null
  const page = Math.floor(offset / limit) + 1
  const pages = Math.ceil(total / limit)
  return (
    <nav className="pagination" aria-label="Pagination">
      <span className="pagination__summary">
        {offset + 1}–{Math.min(offset + limit, total)} of {total} {label}
      </span>
      <Button size="sm" icon="arrowLeft" disabled={page <= 1} onClick={() => onChange(Math.max(0, offset - limit))}>
        Previous
      </Button>
      <span aria-current="page" className="pagination__page">
        Page {page} of {pages}
      </span>
      <Button size="sm" disabled={page >= pages} onClick={() => onChange(offset + limit)}>
        Next
      </Button>
    </nav>
  )
}
