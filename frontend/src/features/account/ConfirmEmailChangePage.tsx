import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { Alert, Spinner } from '../../components/ui'
import { errorMessage } from '../../lib/api/errors'
import { AuthLayout } from '../auth/pages'
import { useAuth } from '../auth/AuthProvider'
import { confirmEmailChange } from './api'

/** Opened from the link sent to the new address (ADR 0039). */
export function ConfirmEmailChangePage() {
  const [params] = useSearchParams()
  const token = params.get('token') ?? ''
  const { status, refreshUser } = useAuth()
  const [state, setState] = useState<'working' | 'done' | 'failed'>(token ? 'working' : 'failed')
  const [message, setMessage] = useState('This link is incomplete.')
  // One-time tokens must be used once, even though StrictMode runs effects twice.
  const started = useRef(false)
  useEffect(() => {
    if (!token || started.current) return
    started.current = true
    confirmEmailChange(token)
      .then((detail) => {
        setMessage(detail)
        setState('done')
      })
      .catch((err: unknown) => {
        setMessage(errorMessage(err))
        setState('failed')
      })
  }, [token])
  useEffect(() => {
    if (state === 'done' && status === 'authenticated') void refreshUser().catch(() => undefined)
  }, [state, status, refreshUser])

  return (
    <AuthLayout title={state === 'done' ? 'Email changed' : state === 'failed' ? "Couldn't change your email" : 'Confirming…'}>
      {state === 'working' && (
        <div className="row">
          <Spinner label="Confirming your new email" /> Confirming your new email…
        </div>
      )}
      {state === 'done' && (
        <>
          <Alert tone="success">{message} Use it the next time you sign in.</Alert>
          <Link className="btn btn--primary btn--md" to={status === 'authenticated' ? '/app' : '/login'}>
            {status === 'authenticated' ? 'Open DocuMind' : 'Sign in'}
          </Link>
        </>
      )}
      {state === 'failed' && (
        <>
          <Alert>{message}</Alert>
          <p className="muted">Links expire after a day. Sign in and ask for a new one on your profile page.</p>
          <Link className="btn btn--secondary btn--md" to="/login">
            Go to sign in
          </Link>
        </>
      )}
    </AuthLayout>
  )
}
