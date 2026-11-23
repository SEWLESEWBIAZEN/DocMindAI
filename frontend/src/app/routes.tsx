import type { ReactElement } from 'react'

import { LandingPage } from '../features/landing/LandingPage'
import { SystemStatusPage } from '../features/system-status/SystemStatusPage'
import { ComingSoonPage } from './ComingSoonPage'

/**
 * Minimal path-based page selection. A full router (protected routes, nested
 * layouts) is introduced with the application shell in Phase 7.
 */
export function resolvePage(pathname: string): ReactElement {
  const path = pathname.replace(/\/+$/, '') || '/'
  switch (path) {
    case '/status':
      return <SystemStatusPage />
    case '/login':
      return <ComingSoonPage action="sign-in" />
    case '/register':
      return <ComingSoonPage action="sign-up" />
    default:
      return <LandingPage />
  }
}
