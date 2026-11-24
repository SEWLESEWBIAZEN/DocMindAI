import { useQueryClient } from '@tanstack/react-query'
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

import { apiClient } from '../../lib/api/client'
import { authClient, clearSession, onSessionEvent, refreshSession, setSession } from '../../lib/api/session'
import type { Schemas, User } from '../../lib/api/types'

type Status = 'loading' | 'authenticated' | 'anonymous'

interface AuthContextValue {
  status: Status
  user: User | null
  /** True when the session ended on its own (expired or revoked). */
  expired: boolean
  signIn: (email: string, password: string) => Promise<User>
  signOut: (options?: { everywhere?: boolean }) => Promise<void>
  setUser: (user: User) => void
  refreshUser: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState<Status>('loading')
  const [user, setUserState] = useState<User | null>(null)
  const [expired, setExpired] = useState(false)

  useEffect(() => {
    let active = true
    // Silent sign-in from the refresh cookie.
    refreshSession()
      .then((token) => {
        if (!active) return
        setUserState(token.user)
        setStatus('authenticated')
      })
      .catch(() => {
        if (active) setStatus('anonymous')
      })
    const unsubscribe = onSessionEvent((event) => {
      if (event.type === 'expired') {
        setUserState(null)
        setExpired(true)
        setStatus('anonymous')
        queryClient.clear()
      }
    })
    return () => {
      active = false
      unsubscribe()
    }
  }, [queryClient])

  const signIn = useCallback(async (email: string, password: string) => {
    const { data } = await apiClient.post<Schemas['TokenResponse']>(
      '/auth/login',
      { email, password },
      { skipAuth: true, withCredentials: true },
    )
    setSession(data)
    setUserState(data.user)
    setExpired(false)
    setStatus('authenticated')
    return data.user
  }, [])

  const signOut = useCallback(
    async (options?: { everywhere?: boolean }) => {
      try {
        if (options?.everywhere) await apiClient.post('/auth/logout-all')
        else await authClient.post('/logout')
      } catch {
        // Signing out locally must work even if the server is unreachable.
      } finally {
        clearSession()
        queryClient.clear()
        setUserState(null)
        setExpired(false)
        setStatus('anonymous')
      }
    },
    [queryClient],
  )

  const refreshUser = useCallback(async () => {
    const { data } = await apiClient.get<User>('/me')
    setUserState(data)
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({ status, user, expired, signIn, signOut, setUser: setUserState, refreshUser }),
    [status, user, expired, signIn, signOut, refreshUser],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth must be used inside <AuthProvider>')
  return value
}

/** The signed-in user (only use below a protected route). */
export function useUser(): User {
  const { user } = useAuth()
  if (!user) throw new Error('useUser requires a signed-in user')
  return user
}
