import { useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router'

import { Alert, announce, Button, Dialog, Field, TextInput } from '../../components/ui'
import { errorMessage } from '../../lib/api/errors'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { useUser } from '../auth/AuthProvider'
import { getPasskey, passkeyErrorMessage } from '../../lib/webauthn'
import { passkeyOptions, sendEmailCode, stepUp } from './api'

/**
 * Asks for a 2FA code (or a recovery code) and hands back a short-lived
 * elevated token for exactly one sensitive action.
 */
export function StepUpDialog({
  open,
  title,
  description,
  actionLabel,
  onClose,
  onVerified,
}: {
  open: boolean
  title: string
  description: string
  actionLabel: string
  onClose: () => void
  onVerified: (token: string) => Promise<void>
}) {
  const user = useUser()
  const { base, workspace } = useWorkspace()
  // ADR 0028: some organizations accept only an authenticator app.
  const emailAllowed = workspace.allow_email_mfa !== false
  const appOnlyButEmailUser = !emailAllowed && user.mfa_method === 'email'
  const [code, setCode] = useState('')
  const hasPasskey = (user.mfa_methods ?? []).includes('passkey')
  const [method, setMethod] = useState<'totp' | 'recovery' | 'email'>(user.mfa_method === 'email' ? 'email' : 'totp')
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (open) announce(`${title} ${description} Enter your authentication code to confirm.`)
  }, [open, title, description])

  const close = () => {
    setCode('')
    setError(null)
    setNotice(null)
    setMethod(user.mfa_method === 'email' ? 'email' : 'totp')
    onClose()
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const digits = code.replace(/\s/g, '')
      const token = await stepUp(
        method === 'recovery' ? { recovery_code: code.trim() } : method === 'email' ? { email_code: digits } : { code: digits },
      )
      await onVerified(token)
      close()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const withPasskey = async () => {
    setBusy(true)
    setError(null)
    try {
      const options = await passkeyOptions()
      const token = await stepUp({ passkey: await getPasskey(options) })
      await onVerified(token)
      close()
    } catch (err) {
      setError(passkeyErrorMessage(err) ?? errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  if (!user.mfa_enabled || appOnlyButEmailUser) {
    return (
      <Dialog
        open={open}
        title="Two-factor authentication required"
        description={
          appOnlyButEmailUser
            ? `${workspace.name} accepts only an authenticator app, not codes by email. Set one up in your profile.`
            : 'This action needs two-factor verification. Turn on 2FA in your profile first.'
        }
        onClose={close}
        size="sm"
        footer={
          <>
            <Button onClick={close}>Cancel</Button>
            <Link className="btn btn--primary btn--md" to={`${base}/profile#security`} onClick={close}>
              Set up 2FA
            </Link>
          </>
        }
      />
    )
  }

  return (
    <Dialog open={open} title={title} description={description} onClose={close} size="sm">
      <form className="form" onSubmit={submit}>
        {error && <Alert>{error}</Alert>}
        {notice && <Alert tone="success">{notice}</Alert>}
        {hasPasskey && (
          <>
            <Button variant="primary" icon="key" loading={busy} onClick={() => void withPasskey()}>
              Confirm with a passkey
            </Button>
            <p className="muted">Or enter a code:</p>
          </>
        )}
        <Field
          label={method === 'recovery' ? 'Recovery code' : method === 'email' ? 'Email code' : 'Authentication code'}
          hint={
            method === 'recovery'
              ? 'Each recovery code works once.'
              : method === 'email'
                ? 'The 6-digit code we just emailed you.'
                : 'The 6-digit code from your authenticator app.'
          }
        >
          <TextInput
            value={code}
            onChange={(e) => setCode(e.target.value)}
            inputMode={method === 'recovery' ? 'text' : 'numeric'}
            autoComplete="one-time-code"
            autoFocus
            maxLength={method === 'recovery' ? 32 : 8}
          />
        </Field>
        <div className="stack stack--sm">
          {method !== 'totp' && (user.mfa_methods ?? []).includes('authenticator') && (
            <button type="button" className="link-button" onClick={() => { setMethod('totp'); setCode(''); setNotice(null) }}>
              Use an authenticator code instead
            </button>
          )}
          {emailAllowed && (
          <button
            type="button"
            className="link-button"
            onClick={async () => {
              setError(null)
              try {
                setNotice(await sendEmailCode())
                setMethod('email')
                setCode('')
              } catch (err) {
                setError(errorMessage(err))
              }
            }}
          >
            {method === 'email' ? 'Send a new email code' : 'Email me a code instead'}
          </button>
          )}
          {method !== 'recovery' && (
            <button type="button" className="link-button" onClick={() => { setMethod('recovery'); setCode(''); setNotice(null) }}>
              Use a recovery code instead
            </button>
          )}
        </div>
        <div className="form__actions">
          <Button onClick={close}>Cancel</Button>
          <Button type="submit" variant="danger" loading={busy} disabled={code.trim().length < 6}>
            {actionLabel}
          </Button>
        </div>
      </form>
    </Dialog>
  )
}
