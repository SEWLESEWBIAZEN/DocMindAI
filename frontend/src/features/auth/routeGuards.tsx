import type { ReactNode } from 'react'
import { Navigate, useLocation, useSearchParams } from 'react-router'

import { FullPageLoading } from '../../app/FullPageLoading'
import { useAuth } from './AuthProvider'

/** Only allow in-app, same-origin redirect targets (no open redirects). */
export function safeNext(next: string | null): string {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/\\')) return '/app'
  return next
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useAuth()
  const location = useLocation()
  if (status === 'loading') return <FullPageLoading />
  if (status === 'anonymous') {
    const next = encodeURIComponent(`${location.pathname}${location.search}`)
    return <Navigate to={`/login?next=${next}`} replace />
  }
  return <>{children}</>
}

/** Sign-in and sign-up pages: skip them when already signed in. */
export function RedirectIfAuthenticated({ children }: { children: ReactNode }) {
  const { status } = useAuth()
  const [params] = useSearchParams()
  if (status === 'loading') return <FullPageLoading />
  if (status === 'authenticated') return <Navigate to={safeNext(params.get('next'))} replace />
  return <>{children}</>
}
