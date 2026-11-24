import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState, type FormEvent } from 'react'

import {
  Alert,
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Field,
  Loading,
  PageHeader,
  TextInput,
  useConfirm,
  useToast,
} from '../../components/ui'
import { ApiError, errorMessage, isStatus } from '../../lib/api/errors'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { formatDateTime, formatNumber } from '../documents/format'
import { StepUpDialog } from '../documents/StepUpDialog'
import {
  CAPABILITY_LABELS,
  getProviders,
  getReindex,
  planKey,
  providersKey,
  putProviderKey,
  removeProviderKey,
  revalidateProviderKey,
  ROUTE_LABELS,
  setKeyOnly,
  startReindex,
  type AIProviders,
} from './api'

type Pending =
  | { kind: 'save'; apiKey: string; reindex: boolean }
  | { kind: 'remove'; reindex: boolean }
  | { kind: 'key-only-off' }

function routeTone(route: string) {
  if (route === 'workspace_key') return 'accent' as const
  if (route === 'platform') return 'neutral' as const
  if (route === 'provider_key_invalid') return 'danger' as const
  return 'warning' as const
}

/** Workspace AI keys (BYOK, ADR 0019). Owners and admins. */
export function AIProvidersPage() {
  useEffect(() => {
    document.title = 'AI providers · DocuMind'
  }, [])
  const { workspace, can } = useWorkspace()
  const queryClient = useQueryClient()
  const toast = useToast()
  const confirm = useConfirm()
  const query = useQuery({
    queryKey: providersKey(workspace.id),
    queryFn: () => getProviders(workspace.id),
    enabled: can('ai_providers.manage'),
  })
  const [apiKey, setApiKey] = useState('')
  const [pending, setPending] = useState<Pending | null>(null)
  const [formError, setFormError] = useState<string | null>(null)

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: providersKey(workspace.id) })
    await queryClient.invalidateQueries({ queryKey: planKey(workspace.id) })
  }

  const revalidate = useMutation({
    mutationFn: () => revalidateProviderKey(workspace.id, 'openai'),
    onSuccess: async () => {
      toast.success('The key works again.')
      await refresh()
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  const reindex = useMutation({
    mutationFn: () => startReindex(workspace.id),
    onSuccess: async (r) => {
      toast.success(`Re-indexing ${r.queued} document(s).`)
      await refresh()
    },
    onError: (err) => toast.error(errorMessage(err)),
  })
  const estimate = useQuery({
    queryKey: ['ws', workspace.id, 'reindex'],
    queryFn: () => getReindex(workspace.id),
    enabled: can('ai_providers.manage') && query.isSuccess,
  })

  if (!can('ai_providers.manage') || isStatus(query.error, 403)) {
    return (
      <>
        <PageHeader title="AI providers" />
        <EmptyState icon="lock" title="Only owners and admins manage AI keys" />
      </>
    )
  }
  if (query.isLoading) return <><PageHeader title="AI providers" /><Loading label="Loading AI providers" /></>
  if (query.error || !query.data) {
    return <><PageHeader title="AI providers" /><ErrorState error={query.error} onRetry={() => void query.refetch()} /></>
  }
  const data: AIProviders = query.data
  const openai = data.providers.find((p) => p.provider === 'openai')

  const onVerified = async (token: string) => {
    if (!pending) return
    try {
      if (pending.kind === 'save') {
        await putProviderKey(workspace.id, 'openai', { api_key: pending.apiKey, reindex: pending.reindex }, token)
        setApiKey('')
        toast.success(pending.reindex ? 'Key saved. Re-indexing has started.' : 'Key saved.')
      } else if (pending.kind === 'remove') {
        await removeProviderKey(workspace.id, 'openai', pending.reindex, token)
        toast.success("Key removed. DocuMind's key is used where your plan allows it.")
      } else {
        await setKeyOnly(workspace.id, false, token)
        toast.success("DocuMind's key may be used again.")
      }
      setPending(null)
      await refresh()
    } catch (err) {
      if (err instanceof ApiError && err.code === 'reindex_required' && pending.kind !== 'key-only-off') {
        const ok = await confirm({
          title: 'Re-index your documents?',
          description: `${err.message} Search won't find documents until they are re-indexed.`,
          confirmLabel: 'Save and re-index',
        })
        if (ok) {
          setPending({ ...pending, reindex: true })
          // Same elevated token, still fresh.
          if (pending.kind === 'save') {
            await putProviderKey(workspace.id, 'openai', { api_key: pending.apiKey, reindex: true }, token)
            setApiKey('')
            toast.success('Key saved. Re-indexing has started.')
          } else {
            await removeProviderKey(workspace.id, 'openai', true, token)
            toast.success('Key removed. Re-indexing has started.')
          }
          setPending(null)
          await refresh()
          return
        }
        setPending(null)
        return
      }
      setPending(null)
      if (pending.kind === 'save') setFormError(errorMessage(err))
      else toast.error(errorMessage(err))
    }
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    setFormError(null)
    const value = apiKey.trim()
    if (value.length < 20) {
      setFormError("That doesn't look like an API key.")
      return
    }
    setPending({ kind: 'save', apiKey: value, reindex: false })
  }

  const toggleKeyOnly = async () => {
    if (data.key_only) {
      setPending({ kind: 'key-only-off' })
      return
    }
    const ok = await confirm({
      title: 'Use your key only?',
      description:
        "DocuMind's own AI key will never be used for this workspace. Features your key doesn't cover stop working until a key is added.",
      confirmLabel: 'Use my key only',
    })
    if (!ok) return
    try {
      await setKeyOnly(workspace.id, true)
      toast.success('This workspace now uses only its own key.')
      await refresh()
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  return (
    <>
      <PageHeader
        title="AI providers"
        description="Use your own provider account for this workspace. Calls are billed to you and don't count toward DocuMind's limits."
      />
      <div className="stack" style={{ gap: 24 }}>
        {openai?.status === 'invalid' && (
          <Alert tone="error">
            OpenAI rejected the saved key ({openai.last_error_code ?? 'error'}). AI features that use it are paused; DocuMind
            doesn't switch to another key. Update the key, or check it again after fixing your OpenAI account.
          </Alert>
        )}
        <section className="panel" aria-labelledby="effective-heading">
          <header className="panel__header">
            <div>
              <h2 id="effective-heading">Which key each feature uses</h2>
              <p className="panel__description">Search index model: {data.embedding_model}</p>
            </div>
          </header>
          <div className="panel__body">
            <ul className="route-list">
              {Object.entries(data.effective).map(([capability, route]) => (
                <li key={capability}>
                  <span>{CAPABILITY_LABELS[capability] ?? capability}</span>
                  <Badge tone={routeTone(route)}>{ROUTE_LABELS[route] ?? route}</Badge>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section className="panel" aria-labelledby="openai-heading">
          <header className="panel__header">
            <div>
              <h2 id="openai-heading">OpenAI</h2>
              <p className="panel__description">
                Used for indexing, answers, figure descriptions and speech. The key is stored encrypted and can't be viewed
                again. Saving or removing it needs your two-factor code.
              </p>
            </div>
            {openai && (
              <Badge tone={openai.status === 'active' ? 'success' : 'danger'}>
                {openai.status === 'active' ? 'Active' : 'Rejected'}
              </Badge>
            )}
          </header>
          <div className="panel__body stack">
            {!data.byok_allowed && <Alert tone="warning">Your plan doesn't include using your own AI key.</Alert>}
            {openai && (
              <dl className="meta-list">
                <div>
                  <dt>Key</dt>
                  <dd>
                    <code>{openai.hint}</code>
                  </dd>
                </div>
                <div>
                  <dt>Added</dt>
                  <dd>{formatDateTime(openai.created_at)}</dd>
                </div>
                <div>
                  <dt>Last used</dt>
                  <dd>{openai.last_used_at ? formatDateTime(openai.last_used_at) : 'Not yet'}</dd>
                </div>
                <div>
                  <dt>Models</dt>
                  <dd>
                    {openai.models.embedding} · {openai.models.chat}
                  </dd>
                </div>
              </dl>
            )}
            <form onSubmit={submit} className="stack stack--sm" noValidate>
              <Field
                label={openai ? 'Replace the key' : 'API key'}
                hint="Starts with sk-. Paste it here; it's checked with OpenAI before it's saved."
                error={formError}
              >
                <TextInput
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  disabled={!data.byok_allowed}
                />
              </Field>
              <div className="row">
                <Button type="submit" variant="primary" icon="key" disabled={!data.byok_allowed || !apiKey.trim()}>
                  {openai ? 'Replace key' : 'Save key'}
                </Button>
                {openai?.status === 'invalid' && (
                  <Button onClick={() => revalidate.mutate()} loading={revalidate.isPending}>
                    Check again
                  </Button>
                )}
                {openai && (
                  <Button variant="danger" icon="trash" onClick={() => setPending({ kind: 'remove', reindex: false })}>
                    Remove key
                  </Button>
                )}
              </div>
            </form>
          </div>
        </section>

        <section className="panel" aria-labelledby="keyonly-heading">
          <header className="panel__header">
            <div>
              <h2 id="keyonly-heading">Use my key only</h2>
              <p className="panel__description">
                {data.key_only
                  ? "On: DocuMind's key is never used here, even for features your key doesn't cover."
                  : "Off: DocuMind's key is used only for features that have no key of yours, if your plan allows it."}
              </p>
            </div>
            <Button onClick={() => void toggleKeyOnly()}>{data.key_only ? 'Allow DocuMind key' : 'Use my key only'}</Button>
          </header>
        </section>

        {estimate.data?.required && (
          <section className="panel" aria-labelledby="reindex-heading">
            <header className="panel__header">
              <div>
                <h2 id="reindex-heading">Re-index needed</h2>
                <p className="panel__description">
                  {formatNumber(estimate.data.documents)} document(s) (about {formatNumber(estimate.data.tokens)} tokens) are
                  indexed with another model and don't appear in search until re-indexed with {estimate.data.target_model}.
                </p>
              </div>
              <Button variant="primary" icon="refresh" loading={reindex.isPending} onClick={() => reindex.mutate()}>
                Re-index now
              </Button>
            </header>
          </section>
        )}

        <section className="panel" aria-labelledby="coming-heading">
          <header className="panel__header">
            <div>
              <h2 id="coming-heading">More providers</h2>
              <p className="panel__description">Planned for future releases.</p>
            </div>
          </header>
          <div className="panel__body">
            <ul className="inline-list">
              {data.catalog
                .filter((c) => c.status === 'planned')
                .map((c) => (
                  <li key={c.key}>{c.name}</li>
                ))}
            </ul>
          </div>
        </section>
      </div>
      <StepUpDialog
        open={pending !== null && !(pending.kind !== 'key-only-off' && pending.reindex)}
        title={
          pending?.kind === 'remove'
            ? 'Remove the OpenAI key?'
            : pending?.kind === 'key-only-off'
              ? "Allow DocuMind's key again?"
              : 'Save the OpenAI key?'
        }
        description={
          pending?.kind === 'remove'
            ? "AI features switch to DocuMind's key where your plan allows it, or stop if this workspace uses its key only."
            : pending?.kind === 'key-only-off'
              ? "Features without a key of yours will use DocuMind's key and count toward your plan limits."
              : 'The key is checked with OpenAI, then stored encrypted. It replaces any saved key.'
        }
        actionLabel={pending?.kind === 'remove' ? 'Remove key' : pending?.kind === 'key-only-off' ? 'Allow' : 'Save key'}
        onClose={() => setPending(null)}
        onVerified={onVerified}
      />
    </>
  )
}
