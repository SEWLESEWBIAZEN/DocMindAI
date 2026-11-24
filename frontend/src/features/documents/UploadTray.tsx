import { useEffect } from 'react'
import { Link } from 'react-router'

import { Button, Icon } from '../../components/ui'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { formatBytes } from './format'
import { useUploads } from './UploadManager'

/** Floating panel listing uploads in this session. */
export function UploadTray() {
  const { items, clearFinished, active } = useUploads()
  const { base } = useWorkspace()
  const allSucceeded = items.length > 0 && items.every((item) => item.state === 'done')
  useEffect(() => {
    if (!allSucceeded) return
    const timer = setTimeout(clearFinished, 6000)
    return () => clearTimeout(timer)
  }, [allSucceeded, clearFinished])
  if (items.length === 0) return null
  return (
    <section className="upload-tray" aria-label="Uploads">
      <header className="upload-tray__header">
        <h2 className="upload-tray__title">{active > 0 ? `Uploading ${active} file${active > 1 ? 's' : ''}…` : 'Uploads'}</h2>
        {active === 0 && (
          <Button size="sm" variant="ghost" icon="x" label="Close uploads" onClick={clearFinished} />
        )}
      </header>
      <ul className="upload-tray__list">
        {items.map((item) => (
          <li key={item.id} className={`upload-row upload-row--${item.state}`}>
            <Icon name={item.state === 'done' ? 'check' : item.state === 'failed' ? 'alert' : 'file'} size={16} />
            <div className="upload-row__main">
              <span className="upload-row__name">
                {item.documentId ? <Link to={`${base}/documents/${item.documentId}`}>{item.name}</Link> : item.name}
              </span>
              <span className="upload-row__meta">
                {item.state === 'failed'
                  ? item.error
                  : item.state === 'done'
                    ? 'Uploaded · processing'
                    : `${formatBytes(item.size)} · ${Math.round(item.progress * 100)}%`}
              </span>
              {(item.state === 'uploading' || item.state === 'queued') && (
                <progress
                  className="progress"
                  max={1}
                  value={item.progress}
                  aria-label={`Uploading ${item.name}`}
                />
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}
