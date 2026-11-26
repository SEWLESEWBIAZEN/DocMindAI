/** Organization single sign-on and SCIM (ADR 0036). Owners and admins. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'

import { Alert, Badge, Button, Field, Loading, Select, TextInput, useToast } from '../../components/ui'
import { apiClient } from '../../lib/api/client'
import { errorMessage } from '../../lib/api/errors'
import type { Schemas } from '../../lib/api/types'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { StepUpDialog } from '../documents/StepUpDialog'

type Sso = Schemas['SsoResponse']
type Provider = 'google' | 'oidc'
const GOOGLE = 'https://accounts.google.com'
const elevated = (token: string) => ({ headers: { Authorization: `Bearer ${token}` } })

const STATUS: Record<string, { label: string; tone: 'success' | 'warning' | 'neutral' }> = {
  pending_verification: { label: 'Waiting for domain verification', tone: 'warning' },
  active: { label: 'Active', tone: 'success' },
  disabled: { label: 'Disabled', tone: 'neutral' },
}

export function SsoPanel() {
  const { workspace } = useWorkspace()
  const toast = useToast()
  const queryClient = useQueryClient()
  const key = ['ws', workspace.id, 'sso'] as const
  const query = useQuery({
    queryKey: key,
    queryFn: async () => (await apiClient.get<Sso | null>(`/workspaces/${workspace.id}/sso`)).data,
  })
  const [form, setForm] = useState<{
    provider: Provider
    issuer: string
    client_id: string
    client_secret: string
    domains: string
    default_role: string
    enforced: boolean
  } | null>(null)
  const [pending, setPending] = useState<'save' | 'scim' | null>(null)
  const [scimToken, setScimToken] = useState<string | null>(null)
  const current = query.data ?? null
  const values = form ?? {
    provider: (!current || current.issuer === GOOGLE ? 'google' : 'oidc') as Provider,
    issuer: current && current.issuer !== GOOGLE ? current.issuer : '',
    client_id: current?.client_id ?? '',
    client_secret: '',
    domains: current?.domains.join(', ') ?? '',
    default_role: current?.default_role ?? 'member',
    enforced: current?.enforced ?? false,
  }
  const set = (change: Partial<typeof values>) => setForm({ ...values, ...change })
  const google = values.provider === 'google'
  const issuer = google ? GOOGLE : values.issuer.trim()
  // Shown before the first save too: Google asks for it when the client is created.
  const redirectUri = current?.redirect_uri ?? `${window.location.origin}/api/v1/auth/sso/callback`

  const onVerified = async (token: string) => {
    const action = pending
    setPending(null)
    try {
      if (action === 'save') {
        await apiClient.put(
          `/workspaces/${workspace.id}/sso`,
          {
            issuer,
            client_id: values.client_id.trim(),
            client_secret: values.client_secret || null,
            domains: values.domains.split(/[\s,]+/).filter(Boolean),
            default_role: values.default_role,
            enforced: values.enforced,
          },
          elevated(token),
        )
        toast.success('Single sign-on saved.')
        setForm(null)
      } else if (action === 'scim') {
        const { data } = await apiClient.post<{ token: string }>(`/workspaces/${workspace.id}/sso/scim-token`, null, elevated(token))
        setScimToken(data.token)
      }
      await queryClient.invalidateQueries({ queryKey: key })
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }
  const save = useMutation({ mutationFn: async () => setPending('save') })
  const submit = (e: FormEvent) => {
    e.preventDefault()
    save.mutate()
  }

  return (
    <section className="panel" aria-labelledby="sso-heading">
      <header className="panel__header">
        <div>
          <h2 id="sso-heading">
            Single sign-on {current && <Badge tone={STATUS[current.status].tone}>{STATUS[current.status].label}</Badge>}
          </h2>
          <p className="panel__description">
            Members sign in with your identity provider (OpenID Connect: Okta, Microsoft Entra ID, Google Workspace, Auth0, Keycloak…).
            Included from the Business plan.
          </p>
        </div>
      </header>
      {query.isLoading ? (
        <div className="panel__body">
          <Loading lines={3} />
        </div>
      ) : (
        <form className="panel__body stack" onSubmit={submit} noValidate>
          {current?.status === 'pending_verification' && (
            <Alert tone="info">
              Before single sign-on works, DocuMind support confirms your organization owns {current.domains.join(', ')} (usually with a
              DNS TXT record). This stops anyone claiming a domain that isn't theirs.
            </Alert>
          )}
          <div className="form-grid">
            <Field label="Identity provider">
              <Select value={values.provider} onChange={(e) => set({ provider: e.target.value as Provider })}>
                <option value="google">Google Workspace</option>
                <option value="oidc">Other (OpenID Connect)</option>
              </Select>
            </Field>
            {!google && (
              <Field label="Issuer URL" hint="e.g. https://login.microsoftonline.com/TENANT/v2.0">
                <TextInput value={values.issuer} onChange={(e) => set({ issuer: e.target.value })} autoComplete="off" />
              </Field>
            )}
            <Field label="Client ID">
              <TextInput value={values.client_id} onChange={(e) => set({ client_id: e.target.value })} autoComplete="off" />
            </Field>
            <Field label="Client secret" hint={current?.has_client_secret ? 'Leave empty to keep the saved secret.' : undefined}>
              <TextInput type="password" value={values.client_secret} onChange={(e) => set({ client_secret: e.target.value })} autoComplete="new-password" />
            </Field>
            <Field label="Email domains" hint="Comma-separated, e.g. acme.com, acme.co.uk">
              <TextInput value={values.domains} onChange={(e) => set({ domains: e.target.value })} />
            </Field>
            <Field label="New people join as">
              <Select value={values.default_role} onChange={(e) => set({ default_role: e.target.value })}>
                <option value="member">Member</option>
                <option value="viewer">Viewer</option>
              </Select>
            </Field>
          </div>
          <label className="checkbox">
            <input type="checkbox" checked={values.enforced} onChange={(e) => set({ enforced: e.target.checked })} />
            Require single sign-on for members with these domains (no password sign-in)
          </label>
          {google ? (
            <div className="stack">
              <h3>Set up in Google Cloud</h3>
              <ol className="steps">
                <li>
                  In the Google Cloud console, open <strong>APIs &amp; Services → OAuth consent screen</strong> and choose{' '}
                  <strong>Internal</strong>, so only accounts in your Google Workspace can sign in.
                </li>
                <li>
                  Under <strong>Credentials</strong>, create an <strong>OAuth client ID</strong> of type <strong>Web application</strong>{' '}
                  with this authorized redirect URI: <code>{redirectUri}</code>
                </li>
                <li>Paste its client ID and client secret here, with your Google Workspace domains.</li>
              </ol>
              <p className="muted">
                Only accounts managed by your Google Workspace can sign in. Personal Google accounts are refused, even with a work
                address. Google doesn't tell DocuMind when someone leaves, so remove them under Members. Their single sign-on session
                ends within 12 hours either way.
              </p>
            </div>
          ) : (
            <p className="muted">
              Redirect URI to register at your identity provider: <code>{redirectUri}</code>
            </p>
          )}
          <div>
            <Button type="submit" variant="primary" disabled={!issuer || !values.client_id || !values.domains}>
              Save single sign-on
            </Button>
          </div>
          {current?.status === 'active' && current.issuer !== GOOGLE && (
            <div className="stack">
              <h3>Provisioning (SCIM 2.0)</h3>
              <p className="muted">
                Base URL <code>{current.scim_base_url}</code>. {current.scim_enabled ? `Token ${current.scim_token_prefix}… is active.` : 'No token yet.'}
              </p>
              {scimToken && (
                <Alert tone="success">
                  Copy this token now; it won't be shown again: <code>{scimToken}</code>
                </Alert>
              )}
              <div>
                <Button onClick={() => setPending('scim')}>{current.scim_enabled ? 'Replace SCIM token' : 'Create SCIM token'}</Button>
              </div>
            </div>
          )}
        </form>
      )}
      <StepUpDialog
        open={pending !== null}
        title={pending === 'scim' ? 'Create a SCIM token' : 'Save single sign-on'}
        description="Confirm with your two-factor code."
        actionLabel="Confirm"
        onClose={() => setPending(null)}
        onVerified={onVerified}
      />
    </section>
  )
}
