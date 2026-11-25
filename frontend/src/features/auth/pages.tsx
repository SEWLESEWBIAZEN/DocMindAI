import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'

import { Alert, Button, Field, Spinner, TextArea, TextInput } from '../../components/ui'
import { apiClient } from '../../lib/api/client'
import { ApiError, errorMessage } from '../../lib/api/errors'
import type { Schemas } from '../../lib/api/types'
import { getPasskey, passkeyErrorMessage, type Json } from '../../lib/webauthn'
import { workspacesKey } from '../../lib/workspace/WorkspaceContext'
import { Logo } from '../landing/components/Logo'
import { useAuth } from './AuthProvider'
import { PASSWORD_MAX, PASSWORD_MIN, passwordScore, validatePassword } from './password'
import { safeNext } from './routeGuards'

export function AuthLayout({ title, subtitle, children, footer }: {
  title: string
  subtitle?: ReactNode
  children: ReactNode
  footer?: ReactNode
}) {
  useEffect(() => {
    document.title = `${title} · DocuMind`
  }, [title])
  return (
    <main className="auth-page" id="main">
      <Link to="/" className="auth-page__brand" aria-label="DocuMind home">
        <Logo />
        <span>DocuMind</span>
      </Link>
      <section className="auth-card" aria-labelledby="auth-title">
        <header>
          <h1 id="auth-title" className="auth-card__title">
            {title}
          </h1>
          {subtitle && <p className="auth-card__subtitle">{subtitle}</p>}
        </header>
        {children}
      </section>
      {footer && <p className="auth-footer">{footer}</p>}
    </main>
  )
}

const SCORE_LABEL = ['', 'Too weak', 'Fair', 'Good', 'Strong']

export function PasswordMeter({ password }: { password: string }) {
  const score = passwordScore(password)
  if (!password) return null
  return (
    <div className="stack stack--sm">
      <div className="password-meter" data-score={score} aria-hidden="true">
        <span />
        <span />
        <span />
        <span />
      </div>
      <span className="field__hint" aria-live="polite">
        Strength: {password.length < PASSWORD_MIN ? `needs ${PASSWORD_MIN - password.length} more characters` : SCORE_LABEL[score]}
      </span>
    </div>
  )
}

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

// ------------------------------------------------------------------- login

export function LoginPage() {
  const { signIn, completeSignIn, expired } = useAuth()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [unverified, setUnverified] = useState(false)
  const [busy, setBusy] = useState(false)
  // Second step for accounts with 2FA (ADR 0026).
  const [challenge, setChallenge] = useState<string | null>(null)
  const [code, setCode] = useState('')
  // Authenticator app, a saved recovery code, or a one-time code by email (ADR 0027).
  const [method, setMethod] = useState<'passkey' | 'totp' | 'recovery' | 'email' | 'reset'>('totp')
  const [reason, setReason] = useState('')
  const [notice, setNotice] = useState<string | null>(null)
  const [methods, setMethods] = useState<string[]>([])

  const finish = () => navigate(safeNext(params.get('next')), { replace: true })

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    setUnverified(false)
    setBusy(true)
    try {
      const result = await signIn(email.trim(), password)
      if ('challenge' in result) {
        setChallenge(result.challenge)
        setMethods(result.methods)
        setPassword('')
        setCode('')
        if (result.methods.includes('passkey')) setMethod('passkey')
        // Email-only 2FA (ADR 0028): send the code straight away.
        else if (!result.methods.includes('authenticator')) await emailCode(result.challenge)
      } else finish()
    } catch (err) {
      if (err instanceof ApiError && err.code === 'email_not_verified') setUnverified(true)
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const submitCode = async (event: FormEvent) => {
    event.preventDefault()
    if (!challenge) return
    setError(null)
    setBusy(true)
    try {
      const value = code.trim()
      const digits = value.replace(/\s/g, '')
      await completeSignIn(
        challenge,
        method === 'recovery' ? { recovery_code: value } : method === 'email' ? { email_code: digits } : { code: digits },
      )
      finish()
    } catch (err) {
      if (err instanceof ApiError && err.code === 'mfa_challenge_expired') {
        setChallenge(null)
        setMethod('totp')
      }
      setError(errorMessage(err))
      setCode('')
    } finally {
      setBusy(false)
    }
  }

  const signInWithPasskey = async () => {
    if (!challenge) return
    setError(null)
    setBusy(true)
    try {
      const { data: options } = await apiClient.post<Json>(
        '/auth/login/mfa/passkey-options',
        { challenge_token: challenge },
        { skipAuth: true },
      )
      await completeSignIn(challenge, { passkey: await getPasskey(options) })
      finish()
    } catch (err) {
      if (err instanceof ApiError && err.code === 'mfa_challenge_expired') setChallenge(null)
      setError(passkeyErrorMessage(err) ?? errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const requestReset = async (event: FormEvent) => {
    event.preventDefault()
    if (!challenge) return
    setError(null)
    setBusy(true)
    try {
      const { data } = await apiClient.post<{ detail: string }>(
        '/auth/login/mfa/reset-request',
        { challenge_token: challenge, reason: reason.trim() },
        { skipAuth: true },
      )
      setNotice(data.detail)
      setReason('')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  async function emailCode(current: string | null = challenge) {
    if (!current) return
    setError(null)
    setNotice(null)
    setBusy(true)
    try {
      const { data } = await apiClient.post<{ detail: string }>(
        '/auth/login/mfa/email-code',
        { challenge_token: current },
        { skipAuth: true },
      )
      setMethod('email')
      setCode('')
      setNotice(`${data.detail} Check ${email.trim()}.`)
    } catch (err) {
      if (err instanceof ApiError && err.code === 'mfa_challenge_expired') setChallenge(null)
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  if (challenge) {
    const switchTo = (next: 'passkey' | 'totp' | 'recovery' | 'reset') => {
      setMethod(next)
      setCode('')
      setError(null)
      setNotice(null)
    }
    const others = (
      <div className="stack stack--sm">
        <p className="muted">Other ways to confirm it's you</p>
        {method !== 'passkey' && methods.includes('passkey') && (
          <button type="button" className="link-button" onClick={() => switchTo('passkey')}>
            Use a passkey
          </button>
        )}
        {method !== 'totp' && methods.includes('authenticator') && (
          <button type="button" className="link-button" onClick={() => switchTo('totp')}>
            Use your authenticator app
          </button>
        )}
        <button type="button" className="link-button" onClick={() => void emailCode()} disabled={busy}>
          {method === 'email' ? 'Send a new email code' : 'Email me a code instead'}
        </button>
        {method !== 'recovery' && (
          <button type="button" className="link-button" onClick={() => switchTo('recovery')}>
            Use a recovery code instead
          </button>
        )}
        {method !== 'reset' && (
          <button type="button" className="link-button" onClick={() => switchTo('reset')}>
            Lost access to all of these? Ask support to reset 2FA
          </button>
        )}
        <button
          type="button"
          className="link-button"
          onClick={() => {
            setChallenge(null)
            setError(null)
            setNotice(null)
          }}
        >
          Back
        </button>
      </div>
    )
    const subtitle =
      method === 'passkey'
        ? 'Use the passkey on this device, your phone or a security key.'
        : method === 'recovery'
          ? 'Enter one of the recovery codes you saved when you set up two-factor authentication.'
          : method === 'email'
            ? 'Enter the 6-digit code we emailed you.'
            : method === 'reset'
              ? 'If you lost your authenticator, passkeys, recovery codes and access to your email, support can reset two-factor authentication after verifying your identity.'
              : 'Enter the 6-digit code from your authenticator app.'
    return (
      <AuthLayout title="Two-factor authentication" subtitle={subtitle}>
        {error && <Alert>{error}</Alert>}
        {notice && <Alert tone="success">{notice}</Alert>}
        {method === 'passkey' ? (
          <div className="form">
            <Button variant="primary" icon="key" loading={busy} onClick={() => void signInWithPasskey()}>
              Sign in with a passkey
            </Button>
            {others}
          </div>
        ) : method === 'reset' ? (
          <form className="form" onSubmit={requestReset} noValidate>
            <Field
              label="What happened?"
              hint="Support will contact you to verify your identity. Nothing changes until they approve, and then only after a waiting period; we email your account address at each step."
            >
              <TextArea value={reason} onChange={(e) => setReason(e.target.value)} rows={4} maxLength={1000} required />
            </Field>
            <Button type="submit" variant="primary" loading={busy} disabled={reason.trim().length < 10}>
              Send reset request
            </Button>
            {others}
          </form>
        ) : (
          <form className="form" onSubmit={submitCode} noValidate>
            <Field label={method === 'recovery' ? 'Recovery code' : method === 'email' ? 'Email code' : 'Authentication code'}>
              <TextInput
                value={code}
                onChange={(e) => setCode(e.target.value)}
                autoComplete="one-time-code"
                inputMode={method === 'recovery' ? 'text' : 'numeric'}
                autoFocus
                required
              />
            </Field>
            <Button type="submit" variant="primary" loading={busy} disabled={!code.trim()}>
              Verify and sign in
            </Button>
            {others}
          </form>
        )}
      </AuthLayout>
    )
  }

  return (
    <AuthLayout
      title="Sign in"
      subtitle="Welcome back. Sign in to your documents."
      footer={
        <>
          New to DocuMind? <Link to={`/register${params.get('next') ? `?next=${encodeURIComponent(params.get('next') ?? '')}` : ''}`}>Create an account</Link>
        </>
      }
    >
      {expired && !error && <Alert tone="info">Your session ended. Please sign in again.</Alert>}
      {params.get('reset') === '1' && <Alert tone="success">Password updated. Sign in with your new password.</Alert>}
      {error && (
        <Alert>
          {error}
          {unverified && <ResendVerification email={email} />}
        </Alert>
      )}
      <form className="form" onSubmit={submit} noValidate>
        <Field label="Email">
          <TextInput
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoFocus
          />
        </Field>
        <Field label="Password">
          <TextInput
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <div className="row row--between">
          <Link to="/forgot-password">Forgot password?</Link>
        </div>
        <Button type="submit" variant="primary" loading={busy} disabled={!email || !password}>
          Sign in
        </Button>
      </form>
    </AuthLayout>
  )
}

function ResendVerification({ email }: { email: string }) {
  const [state, setState] = useState<'idle' | 'sending' | 'sent'>('idle')
  if (state === 'sent') return <p>We sent a new verification link to {email}.</p>
  return (
    <p>
      <button
        type="button"
        className="link-button"
        disabled={state === 'sending'}
        onClick={async () => {
          setState('sending')
          await apiClient.post('/auth/verify-email/resend', { email }, { skipAuth: true }).catch(() => undefined)
          setState('sent')
        }}
      >
        Send a new verification link
      </button>
    </p>
  )
}

// ---------------------------------------------------------------- register

export function RegisterPage() {
  const [params] = useSearchParams()
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const next: Record<string, string> = {}
    if (!name.trim()) next.display_name = 'Enter your name.'
    if (!EMAIL.test(email.trim())) next.email = 'Enter a valid email address.'
    const pw = validatePassword(password)
    if (pw) next.password = pw
    setErrors(next)
    setError(null)
    if (Object.keys(next).length) return
    setBusy(true)
    try {
      await apiClient.post(
        '/auth/register',
        { display_name: name.trim(), email: email.trim(), password },
        { skipAuth: true },
      )
      setDone(true)
    } catch (err) {
      if (err instanceof ApiError) setErrors(err.fields)
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  if (done) {
    return (
      <AuthLayout
        title="Check your email"
        subtitle={<>We sent a verification link to <strong>{email.trim()}</strong>. Open it to activate your account.</>}
        footer={<Link to={`/login${params.get('next') ? `?next=${encodeURIComponent(params.get('next') ?? '')}` : ''}`}>Back to sign in</Link>}
      >
        <Alert tone="info">
          Didn't get it? Check spam, or <ResendInline email={email.trim()} />.
        </Alert>
      </AuthLayout>
    )
  }

  return (
    <AuthLayout
      title="Create your account"
      subtitle="Your personal workspace is ready as soon as you verify your email."
      footer={
        <>
          Already have an account? <Link to="/login">Sign in</Link>
        </>
      }
    >
      {error && <Alert>{error}</Alert>}
      <form className="form" onSubmit={submit} noValidate>
        <Field label="Name" error={errors.display_name}>
          <TextInput autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} autoFocus />
        </Field>
        <Field label="Work email" error={errors.email}>
          <TextInput type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field label="Password" hint={`At least ${PASSWORD_MIN} characters. A passphrase works well.`} error={errors.password}>
          <TextInput
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            maxLength={PASSWORD_MAX}
          />
        </Field>
        <PasswordMeter password={password} />
        <Button type="submit" variant="primary" loading={busy}>
          Create account
        </Button>
      </form>
    </AuthLayout>
  )
}

function ResendInline({ email }: { email: string }) {
  const [sent, setSent] = useState(false)
  if (sent) return <>a new link is on its way</>
  return (
    <button
      type="button"
      className="link-button"
      onClick={() => {
        void apiClient.post('/auth/verify-email/resend', { email }, { skipAuth: true }).catch(() => undefined)
        setSent(true)
      }}
    >
      send it again
    </button>
  )
}

// ------------------------------------------------------------ verify email

export function VerifyEmailPage() {
  const [params] = useSearchParams()
  const token = params.get('token') ?? ''
  const { status, refreshUser } = useAuth()
  const [state, setState] = useState<'working' | 'done' | 'failed'>(token ? 'working' : 'failed')
  const [message, setMessage] = useState('This verification link is incomplete.')
  // One-time tokens must be used once, even though StrictMode runs effects twice.
  const started = useRef(false)
  useEffect(() => {
    if (!token || started.current) return
    started.current = true
    apiClient
      .post('/auth/verify-email', { token }, { skipAuth: true })
      .then(() => setState('done'))
      .catch((err: unknown) => {
        setMessage(errorMessage(err))
        setState('failed')
      })
  }, [token])
  useEffect(() => {
    if (state === 'done' && status === 'authenticated') void refreshUser().catch(() => undefined)
  }, [state, status, refreshUser])

  return (
    <AuthLayout title={state === 'done' ? 'Email verified' : state === 'failed' ? "Couldn't verify" : 'Verifying…'}>
      {state === 'working' && (
        <div className="row">
          <Spinner label="Verifying your email" /> Verifying your email…
        </div>
      )}
      {state === 'done' && (
        <>
          <Alert tone="success">Your email is verified. You're ready to go.</Alert>
          <Link className="btn btn--primary btn--md" to={status === 'authenticated' ? '/app' : '/login'}>
            {status === 'authenticated' ? 'Open DocuMind' : 'Sign in'}
          </Link>
        </>
      )}
      {state === 'failed' && (
        <>
          <Alert>{message}</Alert>
          <p className="muted">Links expire after a while. Sign in to request a new one.</p>
          <Link className="btn btn--secondary btn--md" to="/login">
            Go to sign in
          </Link>
        </>
      )}
    </AuthLayout>
  )
}

// ----------------------------------------------------------------- forgot

export function ForgotPasswordPage() {
  const [email, setEmail] = useState('')
  const [sent, setSent] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!EMAIL.test(email.trim())) {
      setError('Enter a valid email address.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await apiClient.post('/auth/password/forgot', { email: email.trim() }, { skipAuth: true })
      setSent(true)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthLayout
      title="Reset your password"
      subtitle="We'll email you a link to choose a new password."
      footer={<Link to="/login">Back to sign in</Link>}
    >
      {sent ? (
        <Alert tone="success">
          If an account exists for <strong>{email.trim()}</strong>, a reset link is on its way. It expires soon, so use
          it today.
        </Alert>
      ) : (
        <form className="form" onSubmit={submit} noValidate>
          {error && <Alert>{error}</Alert>}
          <Field label="Email">
            <TextInput type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
          </Field>
          <Button type="submit" variant="primary" loading={busy}>
            Send reset link
          </Button>
        </form>
      )}
    </AuthLayout>
  )
}

// ------------------------------------------------------------------ reset

export function ResetPasswordPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const token = params.get('token') ?? ''
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const next: Record<string, string> = {}
    const pw = validatePassword(password)
    if (pw) next.new_password = pw
    if (confirm !== password) next.confirm = "Passwords don't match."
    setErrors(next)
    if (Object.keys(next).length) return
    setBusy(true)
    setError(null)
    try {
      await apiClient.post('/auth/password/reset', { token, new_password: password }, { skipAuth: true })
      navigate('/login?reset=1', { replace: true })
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  if (!token) {
    return (
      <AuthLayout title="Invalid link" footer={<Link to="/forgot-password">Request a new link</Link>}>
        <Alert>This reset link is incomplete. Request a new one.</Alert>
      </AuthLayout>
    )
  }
  return (
    <AuthLayout title="Choose a new password" subtitle="You'll be signed out on every device." footer={<Link to="/login">Back to sign in</Link>}>
      {error && <Alert>{error}</Alert>}
      <form className="form" onSubmit={submit} noValidate>
        <Field label="New password" hint={`At least ${PASSWORD_MIN} characters.`} error={errors.new_password}>
          <TextInput type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />
        </Field>
        <PasswordMeter password={password} />
        <Field label="Confirm new password" error={errors.confirm}>
          <TextInput type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </Field>
        <Button type="submit" variant="primary" loading={busy}>
          Update password
        </Button>
      </form>
    </AuthLayout>
  )
}

// ------------------------------------------------------- accept invitation

export function AcceptInvitationPage() {
  const [params] = useSearchParams()
  const token = params.get('token') ?? ''
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const [error, setError] = useState<string | null>(token ? null : 'This invitation link is incomplete.')
  const [busy, setBusy] = useState(false)

  const accept = async () => {
    setBusy(true)
    setError(null)
    try {
      const { data } = await apiClient.post<Schemas['AcceptedInvitationResponse']>('/invitations/accept', { token })
      await queryClient.invalidateQueries({ queryKey: workspacesKey })
      navigate(`/w/${data.workspace_id}/dashboard`, { replace: true })
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthLayout
      title="Join a workspace"
      subtitle={<>You're signed in as <strong>{user?.email}</strong>. The invitation must be for this address.</>}
      footer={<Link to="/app">Go to my workspaces</Link>}
    >
      {error && <Alert>{error}</Alert>}
      {token && (
        <Button variant="primary" loading={busy} onClick={() => void accept()}>
          Accept invitation
        </Button>
      )}
    </AuthLayout>
  )
}
