import { useSystemHealth } from './useSystemHealth'
import type { ComponentHealth } from './types'

const COMPONENT_LABELS: Record<string, string> = {
  database: 'PostgreSQL + pgvector',
  redis: 'Redis',
  object_storage: 'Object storage',
}

function StatusDot({ ok }: { ok: boolean }) {
  return <span className={`status-dot ${ok ? 'status-dot--ok' : 'status-dot--error'}`} aria-hidden="true" />
}

function ComponentRow({ name, health }: { name: string; health: ComponentHealth }) {
  const ok = health.status === 'ok'
  const details = Object.entries(health.details ?? {})
  return (
    <li className="component-row">
      <div className="component-row__main">
        <StatusDot ok={ok} />
        <span className="component-row__name">{COMPONENT_LABELS[name] ?? name}</span>
        <span className={`badge ${ok ? 'badge--ok' : 'badge--error'}`}>{ok ? 'Operational' : 'Unavailable'}</span>
      </div>
      <div className="component-row__meta">
        <span>{health.latency_ms.toFixed(1)} ms</span>
        {details.map(([key, value]) => (
          <span key={key}>
            {key.replaceAll('_', ' ')}: <code>{String(value)}</code>
          </span>
        ))}
        {health.error && <span className="component-row__error">{health.error}</span>}
      </div>
    </li>
  )
}

export function SystemStatusPage() {
  const { data, error, isPending, isFetching, refetch, dataUpdatedAt } = useSystemHealth()

  return (
    <main className="page">
      <header className="page__header">
        <a className="brand" href="/">
          <img src="/favicon.svg" alt="" width={28} height={28} />
          <span className="brand__name">DocuMind AI</span>
        </a>
        <button type="button" className="button" onClick={() => void refetch()} disabled={isFetching}>
          {isFetching ? 'Checking…' : 'Refresh'}
        </button>
      </header>

      <section className="card" aria-labelledby="status-heading" aria-busy={isPending}>
        <div className="card__header">
          <h1 id="status-heading">System status</h1>
          {data && (
            <span className={`badge ${data.status === 'ok' ? 'badge--ok' : 'badge--error'}`} role="status">
              {data.status === 'ok' ? 'All systems operational' : 'Degraded'}
            </span>
          )}
        </div>

        {isPending && (
          <ul className="component-list" aria-label="Loading status">
            {[0, 1, 2].map((i) => (
              <li key={i} className="skeleton" />
            ))}
          </ul>
        )}

        {error && !data && (
          <div className="alert" role="alert">
            <strong>Cannot reach the API.</strong> {error.message}
          </div>
        )}

        {data && (
          <>
            <ul className="component-list">
              {Object.entries(data.components).map(([name, health]) => (
                <ComponentRow key={name} name={name} health={health} />
              ))}
            </ul>
            <footer className="card__footer">
              <span>
                {data.app.name} v{data.app.version} · {data.app.environment}
              </span>
              <span>Last checked {new Date(dataUpdatedAt).toLocaleTimeString()}</span>
            </footer>
          </>
        )}
      </section>
    </main>
  )
}
