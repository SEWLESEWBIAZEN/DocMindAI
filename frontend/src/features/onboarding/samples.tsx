/** Sample documents and the getting-started checklist (ADR 0033). */
import { useState, type ReactNode } from 'react'
import { Link } from 'react-router'

import { Button, Icon } from '../../components/ui'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { useSampleActions, useSamples } from './useSamples'

export function LoadSamplesButton({ variant = 'secondary' }: { variant?: 'primary' | 'secondary' }) {
  const { can } = useWorkspace()
  const { load } = useSampleActions()
  if (!can('documents.write')) return null
  return (
    <Button variant={variant} icon="sparkles" loading={load.isPending} onClick={() => load.mutate()}>
      Try sample documents
    </Button>
  )
}

const DISMISS_KEY = (ws: string) => `documind:getting-started:${ws}`

function readDismissed(ws: string): boolean {
  try {
    return window.localStorage.getItem(DISMISS_KEY(ws)) === '1'
  } catch {
    return false
  }
}

interface Step {
  done: boolean
  label: string
  action?: ReactNode
}

/** First-run checklist on the dashboard. Hidden once everything is done or when dismissed. */
export function GettingStarted({
  documents,
  conversations,
  members,
  mfaEnabled,
}: {
  documents: number | undefined
  conversations: number | undefined
  members: number | undefined
  mfaEnabled: boolean
}) {
  const { workspace, base, can } = useWorkspace()
  const [dismissed, setDismissed] = useState(() => readDismissed(workspace.id))
  const samples = useSamples()
  const { remove } = useSampleActions()
  if (dismissed || documents === undefined) return null

  const steps: Step[] = [
    {
      done: documents > 0,
      label: 'Add documents',
      action: can('documents.write') && (
        <span className="row">
          <Link className="btn btn--primary btn--sm" to={`${base}/upload`}>
            Upload
          </Link>
          <span className="muted">or</span>
          <LoadSamplesButton />
        </span>
      ),
    },
    {
      done: (conversations ?? 0) > 0,
      label: 'Ask your first question',
      action: can('conversations.use') && (
        <Link className="btn btn--secondary btn--sm" to={`${base}/chat`}>
          Ask a question
        </Link>
      ),
    },
    ...(workspace.kind === 'organization' && can('members.invite')
      ? [
          {
            done: (members ?? 0) > 1,
            label: 'Invite your team',
            action: (
              <Link className="btn btn--secondary btn--sm" to={`${base}/members`}>
                Invite people
              </Link>
            ),
          },
        ]
      : []),
    {
      done: mfaEnabled,
      label: 'Protect your account with two-factor authentication',
      action: (
        <Link className="btn btn--secondary btn--sm" to={`${base}/profile#security`}>
          Set up 2FA
        </Link>
      ),
    },
  ]
  const remaining = steps.filter((s) => !s.done).length
  if (remaining === 0 && !samples.data?.loaded) return null

  const dismiss = () => {
    try {
      window.localStorage.setItem(DISMISS_KEY(workspace.id), '1')
    } catch {
      // Private mode: hidden for this visit only.
    }
    setDismissed(true)
  }

  return (
    <section className="panel getting-started" aria-labelledby="getting-started">
      <header className="panel__header">
        <div>
          <h2 id="getting-started">Getting started</h2>
          <p className="panel__description">
            {remaining === 0 ? 'All done.' : `${steps.length - remaining} of ${steps.length} done.`}
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={dismiss}>
          Hide
        </Button>
      </header>
      <ol className="panel__body checklist">
        {steps.map((step) => (
          <li key={step.label} className={step.done ? 'checklist__item checklist__item--done' : 'checklist__item'}>
            <span className="checklist__mark" aria-hidden="true">
              {step.done && <Icon name="check" size={14} />}
            </span>
            <span className="checklist__label">
              {step.label}
              <span className="sr-only">{step.done ? ' (done)' : ' (to do)'}</span>
            </span>
            {!step.done && step.action}
          </li>
        ))}
      </ol>
      {samples.data?.loaded && can('documents.write') && (
        <p className="panel__body getting-started__samples muted">
          This workspace has DocuMind's sample documents.{' '}
          <button type="button" className="link-button" disabled={remove.isPending} onClick={() => remove.mutate()}>
            Remove them
          </button>{' '}
          when you no longer need them.
        </p>
      )}
    </section>
  )
}
