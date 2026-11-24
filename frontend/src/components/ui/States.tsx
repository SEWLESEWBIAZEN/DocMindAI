import type { ReactNode } from 'react'

import { errorMessage, toApiError } from '../../lib/api/errors'
import { Button } from './Button'
import { Icon, type IconName } from './Icon'

export function Skeleton({ lines = 3, height = 18 }: { lines?: number; height?: number }) {
  return (
    <div className="skeleton-group" aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="skeleton-line" style={{ height, width: `${100 - ((i * 17) % 40)}%` }} />
      ))}
    </div>
  )
}

/** Accessible loading placeholder: announced once, skeleton hidden from AT. */
export function Loading({ label = 'Loading', lines = 3 }: { label?: string; lines?: number }) {
  return (
    <div role="status" aria-live="polite" className="loading">
      <span className="sr-only">{label}…</span>
      <Skeleton lines={lines} />
    </div>
  )
}

export function EmptyState({
  icon = 'file',
  title,
  description,
  action,
}: {
  icon?: IconName
  title: string
  description?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="empty-state">
      <span className="empty-state__icon">
        <Icon name={icon} size={22} />
      </span>
      <h2 className="empty-state__title">{title}</h2>
      {description && <p className="empty-state__description">{description}</p>}
      {action && <div className="empty-state__action">{action}</div>}
    </div>
  )
}

export function ErrorState({ error, onRetry, title }: { error: unknown; onRetry?: () => void; title?: string }) {
  const apiError = toApiError(error)
  const heading =
    title ??
    (apiError.status === 404 ? 'Not found' : apiError.status === 403 ? "You don't have access" : "Couldn't load this")
  return (
    <div className="error-state" role="alert">
      <span className="error-state__icon">
        <Icon name="alert" size={22} />
      </span>
      <div>
        <h2 className="error-state__title">{heading}</h2>
        <p className="error-state__message">{errorMessage(error)}</p>
        {apiError.requestId && <p className="error-state__meta">Reference: {apiError.requestId}</p>}
      </div>
      {onRetry && apiError.status !== 404 && apiError.status !== 403 && (
        <Button icon="refresh" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  )
}

export function Alert({ tone = 'error', children }: { tone?: 'error' | 'info' | 'success' | 'warning'; children: ReactNode }) {
  return (
    <div className={`alert alert--${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
      <Icon name={tone === 'success' ? 'check' : tone === 'info' ? 'info' : 'alert'} />
      <div>{children}</div>
    </div>
  )
}
