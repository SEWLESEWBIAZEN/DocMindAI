import { useState, type FormEvent } from 'react'
import { Link } from 'react-router'

import { Alert, Button, Dialog, Field, TextInput } from '../../components/ui'
import { errorMessage } from '../../lib/api/errors'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { useUser } from '../auth/AuthProvider'
import { stepUp } from './api'

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
  const { base } = useWorkspace()
  const [code, setCode] = useState('')
  const [useRecovery, setUseRecovery] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const close = () => {
    setCode('')
    setError(null)
    setUseRecovery(false)
    onClose()
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const token = await stepUp(useRecovery ? { recovery_code: code.trim() } : { code: code.replace(/\s/g, '') })
      await onVerified(token)
      close()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  if (!user.mfa_enabled) {
    return (
      <Dialog
        open={open}
        title="Two-factor authentication required"
        description="This action needs two-factor verification. Turn on 2FA in your profile first."
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
        <Field
          label={useRecovery ? 'Recovery code' : 'Authentication code'}
          hint={useRecovery ? 'Each recovery code works once.' : 'The 6-digit code from your authenticator app.'}
        >
          <TextInput
            value={code}
            onChange={(e) => setCode(e.target.value)}
            inputMode={useRecovery ? 'text' : 'numeric'}
            autoComplete="one-time-code"
            autoFocus
            maxLength={useRecovery ? 32 : 8}
          />
        </Field>
        <button type="button" className="link-button" onClick={() => setUseRecovery((v) => !v)}>
          {useRecovery ? 'Use an authenticator code instead' : 'Use a recovery code instead'}
        </button>
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
