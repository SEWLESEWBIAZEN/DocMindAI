import { useState, type ReactNode } from 'react'
import { Link, useLocation } from 'react-router'

import { Alert, EmptyState, PageHeader } from '../../components/ui'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { useUser } from '../auth/AuthProvider'
import { formatDate } from '../documents/format'

/**
 * Required 2FA (ADR 0026). Workspaces on a paid plan, or whose organization asks
 * for it, give members without 2FA a grace period (a banner), then lock their
 * content until they set it up. Profile stays reachable, so they always can.
 */
export function MfaGate({ children }: { children: ReactNode }) {
  const { workspace, base } = useWorkspace()
  const user = useUser()
  const location = useLocation()
  // Evaluated once per mount; the server enforces the deadline on every request anyway.
  const [now] = useState(() => Date.now())
  const deadline = workspace.mfa_enrollment_deadline
  // An authenticator always counts; email codes only where the workspace allows them.
  if (!deadline || user.mfa_method === 'authenticator' || (user.mfa_method === 'email' && workspace.allow_email_mfa !== false))
    return <>{children}</>
  const appNeeded = user.mfa_method === 'email'

  const reason = workspace.mfa_required_by === 'plan' ? "This workspace's plan" : `${workspace.name}`
  const profile = `${base}/profile`
  const due = new Date(deadline)
  if (due.getTime() <= now && !location.pathname.startsWith(profile)) {
    return (
      <>
        <PageHeader title="Two-factor authentication required" />
        <EmptyState
          icon="shield"
          title={appNeeded ? 'Set up an authenticator app to continue' : 'Set up two-factor authentication to continue'}
          description={
            appNeeded
              ? `${reason} requires two-factor authentication with an authenticator app; codes by email don't count here.`
              : `${reason} requires two-factor authentication for every member. It takes about a minute, and you'll get recovery codes in case you lose your phone.`
          }
          action={
            <Link className="btn btn--primary btn--md" to={profile}>
              Set up two-factor authentication
            </Link>
          }
        />
      </>
    )
  }
  return (
    <>
      {!location.pathname.startsWith(profile) && (
        <div className="mfa-banner">
          <Alert tone="warning">
            {reason} requires two-factor authentication{appNeeded ? ' with an authenticator app (email codes don\'t count here)' : ''}.
            Set it up by <strong>{formatDate(deadline)}</strong> to keep access. <Link to={profile}>Set it up now</Link>
          </Alert>
        </div>
      )}
      {children}
    </>
  )
}
