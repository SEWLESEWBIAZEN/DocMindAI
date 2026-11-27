import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router'

import {
  Alert,
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Icon,
  Loading,
  Menu,
  PageHeader,
  Pagination,
  TextInput,
  useConfirm,
  useToast,
} from '../../components/ui'
import { ApiError, errorMessage } from '../../lib/api/errors'
import type { ChatMessage, Collection, ConversationDetail, DocumentItem } from '../../lib/api/types'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { LoadSamplesButton } from '../onboarding/samples'
import { useSamples } from '../onboarding/useSamples'
import { useUser } from '../auth/AuthProvider'
import { documentKeys, getDocument, listDocuments } from '../documents/api'
import { formatRelative } from '../documents/format'
import { useCollections } from '../documents/hooks'
import {
  cancelReply,
  conversationKeys,
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
  listMessages,
  regenerate,
  renameConversation,
  sendMessage,
  setConversationSources,
  type StreamEvent,
} from './api'
import { friendlyDictationError, startDictation, type Dictation } from '../voice/dictation'
import { answerToSpeech, completedSentences, sourcesSummary, splitSentences, withoutMarkers } from '../voice/text'
import { useVoice } from '../voice/VoiceContext'
import { AnswerFeedback } from './AnswerFeedback'
import { AnswerMarkdown } from './AnswerMarkdown'
import { CitationCard } from './CitationCard'

const MAX_CHARS = 2000

function useTitle(title: string) {
  useEffect(() => {
    document.title = `${title} · DocuMind`
  }, [title])
}

interface Sources {
  documentIds: string[]
  collectionIds: string[]
}

interface LiveReply {
  /** The question being answered (null when regenerating). */
  user: ChatMessage | null
  pendingQuestion: string | null
  assistant: ChatMessage | null
  text: string
  stopping: boolean
}

function splitIds(value: string | null): string[] {
  return value ? value.split(',').filter(Boolean) : []
}

// --------------------------------------------------------------------- chat

export function ChatPage() {
  const { conversationId: routeId } = useParams()
  const { workspace, base, can } = useWorkspace()
  const [params] = useSearchParams()
  const location = useLocation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()
  const confirm = useConfirm()
  const ws = workspace.id

  // The conversation this view shows. It changes without a remount when the
  // first question creates the conversation.
  const [activeId, setActiveId] = useState<string | null>(routeId ?? null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [olderCursor, setOlderCursor] = useState<number | null>(null)
  const [live, setLive] = useState<LiveReply | null>(null)
  const [draft, setDraft] = useState('')
  const [newSources, setNewSources] = useState<Sources>({
    documentIds: splitIds(params.get('documents')),
    collectionIds: splitIds(params.get('collections')),
  })
  const [pickerOpen, setPickerOpen] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [activeCitation, setActiveCitation] = useState<{ message: string; n: number } | null>(null)
  const [announcement, setAnnouncement] = useState('')
  const controller = useRef<AbortController | null>(null)
  const voice = useVoice()
  // Reading a streamed answer aloud as it arrives (opt-in).
  const reading = useRef<{ active: boolean; offset: number; text: string }>({ active: false, offset: 0, text: '' })
  const scroller = useRef<HTMLDivElement>(null)
  const input = useRef<HTMLTextAreaElement>(null)
  const streaming = live !== null

  // Route changed to another conversation (or "new"): reset the view.
  useEffect(() => {
    const target = routeId ?? null
    if (target === activeId) return
    controller.current?.abort()
    setActiveId(target)
    setMessages([])
    setOlderCursor(null)
    setLive(null)
    setDraft('')
    setActiveCitation(null)
    if (!target) {
      setNewSources({ documentIds: splitIds(params.get('documents')), collectionIds: splitIds(params.get('collections')) })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeId, location.state])

  // Leaving the page closes the stream; the server records the answer as cancelled.
  useEffect(() => () => controller.current?.abort(), [])

  const detail = useQuery({
    queryKey: conversationKeys.detail(ws, activeId ?? ''),
    queryFn: () => getConversation(ws, activeId ?? ''),
    enabled: Boolean(activeId),
  })
  const history = useQuery({
    queryKey: conversationKeys.messages(ws, activeId ?? ''),
    queryFn: () => listMessages(ws, activeId ?? ''),
    enabled: Boolean(activeId),
    staleTime: Infinity,
  })
  useEffect(() => {
    if (history.data && !streaming) {
      setMessages(history.data.items)
      setOlderCursor(history.data.next_before)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [history.data])

  useTitle(detail.data?.title ?? 'Ask')

  const scrollToEnd = useCallback(() => {
    requestAnimationFrame(() => {
      const node = scroller.current
      if (node) node.scrollTop = node.scrollHeight
    })
  }, [])
  useEffect(scrollToEnd, [messages.length, live?.text, scrollToEnd])

  const persist = (conversationId: string, update: (items: ChatMessage[]) => ChatMessage[]) => {
    setMessages((current) => {
      const next = update(current)
      queryClient.setQueryData(conversationKeys.messages(ws, conversationId), {
        items: next,
        has_more: olderCursor !== null,
        next_before: olderCursor,
      })
      return next
    })
  }

  const refreshLists = () => {
    void queryClient.invalidateQueries({ queryKey: ['ws', ws, 'conversations', 'list'] })
  }

  const runStream = async (
    conversationId: string,
    start: (options: { signal: AbortSignal; onEvent: (event: StreamEvent) => void }) => Promise<void>,
    restoreDraft?: string,
  ) => {
    const abort = new AbortController()
    controller.current = abort
    let created = false
    reading.current = {
      active: Boolean(voice.prefs.autoRead && voice.ttsEngine && voice.openStream('answer')),
      offset: 0,
      text: '',
    }
    const onEvent = (event: StreamEvent) => {
      switch (event.type) {
        case 'message.created':
          created = true
          setLive((current) => (current ? { ...current, user: event.user_message, assistant: event.assistant_message, pendingQuestion: null } : current))
          if (event.user_message) {
            const userMessage = event.user_message
            persist(conversationId, (items) => [...items, userMessage])
          }
          break
        case 'answer.delta': {
          setLive((current) => (current ? { ...current, text: current.text + event.text } : current))
          const r = reading.current
          r.text += event.text
          if (r.active) {
            const { sentences, offset } = completedSentences(r.text, r.offset)
            r.offset = offset
            voice.append(sentences)
          }
          break
        }
        case 'answer.completed': {
          const final = event.message
          persist(conversationId, (items) => [...items.filter((m) => m.id !== final.id), final])
          setLive(null)
          const r = reading.current
          if (r.active) {
            voice.append(splitSentences(withoutMarkers(r.text.slice(r.offset))))
            const sources = voice.prefs.speakCitations ? sourcesSummary(final.citations) : null
            if (sources) voice.append([sources])
            voice.closeStream()
            r.active = false
          }
          const count = final.citations.length
          setAnnouncement(
            final.answer_status === 'insufficient_context'
              ? 'Answer complete. The documents did not contain this information.'
              : `Answer complete with ${count} ${count === 1 ? 'source' : 'sources'}.`,
          )
          break
        }
        case 'answer.cancelled':
          if (event.message) {
            const stopped = event.message
            persist(conversationId, (items) => [...items, stopped])
          }
          setLive(null)
          setAnnouncement('Answer stopped.')
          if (reading.current.active) voice.stop()
          reading.current.active = false
          break
        case 'answer.error':
          if (event.message) {
            const failed = event.message
            persist(conversationId, (items) => [...items, failed])
          }
          setLive(null)
          toast.error(event.detail)
          if (reading.current.active) voice.stop()
          reading.current.active = false
          break
      }
    }
    try {
      await start({ signal: abort.signal, onEvent })
    } catch (err) {
      if (reading.current.active) voice.stop()
      reading.current.active = false
      if (abort.signal.aborted) return
      setLive(null)
      toast.error(
        err instanceof ApiError && err.code === 'reply_in_progress'
          ? 'An answer is still being generated in this conversation. Wait for it or stop it first.'
          : errorMessage(err),
      )
      if (!created && restoreDraft) setDraft(restoreDraft)
    } finally {
      if (controller.current === abort) controller.current = null
      setLive((current) => (current && !abort.signal.aborted ? null : current))
      refreshLists()
      void queryClient.invalidateQueries({ queryKey: conversationKeys.detail(ws, conversationId) })
    }
  }

  const ask = async (question: string) => {
    const content = question.trim()
    if (!content || streaming) return
    setDraft('')
    setLive({ user: null, pendingQuestion: content, assistant: null, text: '', stopping: false })
    let conversationId = activeId
    if (!conversationId) {
      try {
        const created = await createConversation(ws, {
          document_ids: newSources.documentIds,
          collection_ids: newSources.collectionIds,
        })
        conversationId = created.id
        queryClient.setQueryData(conversationKeys.messages(ws, created.id), { items: [], has_more: false, next_before: null })
        queryClient.setQueryData(conversationKeys.detail(ws, created.id), created)
        setActiveId(created.id)
        navigate(`${base}/chat/${created.id}`, { replace: true })
      } catch (err) {
        setLive(null)
        setDraft(content)
        toast.error(errorMessage(err))
        return
      }
    }
    const id = conversationId
    await runStream(id, (options) => sendMessage(ws, id, content, options), content)
  }

  const stop = async () => {
    if (!live) return
    if (reading.current.active) voice.stop()
    reading.current.active = false
    setLive({ ...live, stopping: true })
    const assistantId = live.assistant?.id
    if (activeId && assistantId) {
      try {
        await cancelReply(ws, activeId, assistantId)
        return // the stream ends with answer.cancelled
      } catch {
        // fall through: closing the connection also cancels
      }
    }
    controller.current?.abort()
    setLive(null)
  }

  const regenerateLatest = async (message: ChatMessage) => {
    if (!activeId || streaming) return
    const id = activeId
    setMessages((items) => items.filter((m) => m.id !== message.id))
    setLive({ user: null, pendingQuestion: null, assistant: null, text: '', stopping: false })
    await runStream(id, (options) => regenerate(ws, id, message.id, options))
  }

  // A question handed over from elsewhere (the home page's ask box) is sent once.
  const handedOver = useRef<string | null>((location.state as { ask?: string } | null)?.ask ?? null)
  useEffect(() => {
    const question = handedOver.current
    if (!question || routeId) return
    handedOver.current = null
    void ask(question)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeId])

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void ask(draft)
    }
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void ask(draft)
  }

  const loadOlder = async () => {
    if (!activeId || olderCursor === null) return
    try {
      const page = await listMessages(ws, activeId, olderCursor)
      setMessages((items) => [...page.items, ...items])
      setOlderCursor(page.next_before)
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  const remove = async () => {
    if (!activeId) return
    const ok = await confirm({
      title: 'Delete this conversation?',
      description: 'Its messages and citations are deleted. Your documents are not affected.',
      confirmLabel: 'Delete',
      danger: true,
    })
    if (!ok) return
    try {
      await deleteConversation(ws, activeId)
      refreshLists()
      toast.success('Conversation deleted.')
      navigate(`${base}/conversations`, { replace: true })
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  const saveSources = async (sources: Sources) => {
    if (!activeId) {
      setNewSources(sources)
      return
    }
    try {
      const updated = await setConversationSources(ws, activeId, {
        document_ids: sources.documentIds,
        collection_ids: sources.collectionIds,
      })
      queryClient.setQueryData(conversationKeys.detail(ws, activeId), updated)
      toast.success('Sources updated for the next questions.')
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  const currentSources: Sources = activeId
    ? { documentIds: detail.data?.document_ids ?? [], collectionIds: detail.data?.collection_ids ?? [] }
    : newSources
  const latestAssistantId = [...messages].reverse().find((m) => m.role === 'assistant')?.id

  if (!can('conversations.use')) {
    return <ErrorState title="Conversations aren't available" error={new Error('Your role in this workspace cannot ask questions.')} />
  }
  if (activeId && (detail.error || history.error)) {
    return <ErrorState error={detail.error ?? history.error} onRetry={() => void Promise.all([detail.refetch(), history.refetch()])} />
  }

  const empty = messages.length === 0 && !live

  return (
    <div className="chat">
      <header className="chat__header">
        <h1 className="chat__title truncate">{activeId ? detail.data?.title ?? 'Conversation' : 'New conversation'}</h1>
        <SourceSummary detail={activeId ? detail.data : undefined} sources={currentSources} onEdit={() => setPickerOpen(true)} />
        {activeId && (
          <Menu
            label="Conversation actions"
            trigger={<Icon name="more" />}
            triggerClassName="btn btn--ghost btn--md btn--icon"
            items={[
              { label: 'Rename', icon: 'edit', onSelect: () => setRenaming(true) },
              { label: 'Choose sources', icon: 'layers', onSelect: () => setPickerOpen(true) },
              'separator',
              { label: 'Delete conversation', icon: 'trash', danger: true, onSelect: () => void remove() },
            ]}
          />
        )}
      </header>

      <div className="chat__scroll" ref={scroller}>
        {activeId && history.isLoading ? (
          <Loading label="Loading conversation" lines={6} />
        ) : empty ? (
          <Welcome onPick={(q) => void ask(q)} hasSources={currentSources.documentIds.length + currentSources.collectionIds.length > 0} />
        ) : (
          <div className="chat__messages" aria-label="Conversation" role="log" aria-live="off">
            {olderCursor !== null && (
              <Button size="sm" variant="ghost" icon="history" onClick={() => void loadOlder()}>
                Load earlier messages
              </Button>
            )}
            {messages.map((message) =>
              message.role === 'user' ? (
                <UserMessage key={message.id} content={message.content} />
              ) : (
                <AssistantMessage
                  key={message.id}
                  message={message}
                  canRegenerate={!streaming && message.id === latestAssistantId}
                  onRegenerate={() => void regenerateLatest(message)}
                  activeCitation={activeCitation?.message === message.id ? activeCitation.n : null}
                  onCitation={(n) => setActiveCitation({ message: message.id, n })}
                  onUpdated={(updated) =>
                    persist(updated.conversation_id, (items) => items.map((m) => (m.id === updated.id ? updated : m)))
                  }
                />
              ),
            )}
            {live?.pendingQuestion && <UserMessage content={live.pendingQuestion} />}
            {live && <StreamingMessage text={live.text} stopping={live.stopping} />}
          </div>
        )}
      </div>
      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>

      <form className="composer" onSubmit={submit}>
        <div className="composer__box">
          <label htmlFor="composer" className="sr-only">
            Ask a question about your documents
          </label>
          <textarea
            id="composer"
            ref={input}
            className="composer__input"
            rows={1}
            placeholder={streaming ? 'Answering…' : 'Ask a question about your documents…'}
            value={draft}
            maxLength={MAX_CHARS}
            onChange={(event) => {
              setDraft(event.target.value)
              event.target.style.height = 'auto'
              event.target.style.height = `${Math.min(event.target.scrollHeight, 200)}px`
            }}
            onKeyDown={onKeyDown}
            aria-describedby="composer-hint"
          />
          {voice.sttEngine && !streaming && (
            <MicButton
              onText={(text) => {
                if (voice.prefs.sendAfterDictation) {
                  void ask(text)
                } else {
                  setDraft((current) => (current.trim() ? `${current.trim()} ${text}` : text))
                  input.current?.focus()
                }
              }}
            />
          )}
          {streaming ? (
            <Button variant="secondary" icon="stop" onClick={() => void stop()} loading={live?.stopping}>
              Stop
            </Button>
          ) : (
            <Button type="submit" variant="primary" icon="send" label="Send question" disabled={!draft.trim()} />
          )}
        </div>
        <p className="composer__hint" id="composer-hint">
          <span>
            <kbd>Enter</kbd> to send · <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line
          </span>
          <span>Answers cite your documents; check important details in the source.</span>
        </p>
      </form>

      {pickerOpen && (
        <SourcePicker
          initial={currentSources}
          onClose={() => setPickerOpen(false)}
          onSave={(sources) => {
            setPickerOpen(false)
            void saveSources(sources)
          }}
        />
      )}
      {renaming && detail.data && (
        <RenameConversationDialog
          conversation={detail.data}
          onClose={() => setRenaming(false)}
          onSaved={(updated) => {
            queryClient.setQueryData(conversationKeys.detail(ws, updated.id), updated)
            refreshLists()
          }}
        />
      )}
    </div>
  )
}

function Welcome({ onPick, hasSources }: { onPick: (question: string) => void; hasSources: boolean }) {
  const { workspace } = useWorkspace()
  const user = useUser()
  const docs = useQuery({
    queryKey: documentKeys.list(workspace.id, { status: 'ready', limit: 1 }),
    queryFn: () => listDocuments(workspace.id, { status: 'ready', limit: 1 }),
    // A cached "nothing ready yet" goes stale the moment processing finishes: always re-check.
    staleTime: 0,
  })
  const { base, can } = useWorkspace()
  const samples = useSamples()
  const noDocs = docs.data?.total === 0 && !hasSources
  return (
    <div className="chat__welcome">
      <span className="empty-state__icon" aria-hidden="true">
        <Icon name="sparkles" size={22} />
      </span>
      <h2 style={{ fontSize: '1.35rem' }}>What would you like to know, {user.display_name.split(' ')[0]}?</h2>
      <p className="muted" style={{ maxWidth: '52ch' }}>
        {hasSources
          ? 'Answers use only the sources you selected, with a citation for every claim.'
          : 'Answers come from the ready documents in this workspace, with a citation for every claim.'}
      </p>
      {noDocs ? (
        <Alert tone="info">
          There are no processed documents yet.{' '}
          {can('documents.write') ? (
            <>
              <Link to={`${base}/upload`}>Upload some</Link>, or try DocuMind with sample documents.
              <span className="row" style={{ marginTop: 8 }}>
                <LoadSamplesButton />
              </span>
            </>
          ) : (
            'Ask an admin to upload some.'
          )}
        </Alert>
      ) : (
        <div className="suggestions">
          {(samples.data?.loaded && !hasSources
            ? samples.data.questions.slice(0, 4)
            : [
                'Summarize the key points of these documents.',
                'What are the main obligations and deadlines?',
                'Are there any risks or open issues mentioned?',
                'What changed compared to the previous version?',
              ]
          ).map((q) => (
            <button key={q} type="button" className="suggestion" onClick={() => onPick(q)}>
              {q}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function UserMessage({ content }: { content: string }) {
  return (
    <div className="msg msg--user">
      <span className="sr-only">You asked:</span>
      <div className="msg__bubble">{content}</div>
    </div>
  )
}

function StreamingMessage({ text, stopping }: { text: string; stopping: boolean }) {
  return (
    <article className="msg" aria-busy="true" aria-label="Answer in progress">
      <div className="msg__role">
        <span className="msg__role-icon" aria-hidden="true">
          <Icon name="sparkles" size={13} />
        </span>
        DocuMind
        <span className="muted">{stopping ? 'stopping…' : text ? 'writing…' : 'searching your documents…'}</span>
      </div>
      <div className="msg__answer">
        {text ? (
          <div className="markdown">
            <p style={{ whiteSpace: 'pre-wrap' }}>
              {text}
              <span className="streaming-caret" aria-hidden="true" />
            </p>
          </div>
        ) : (
          <Loading label="Finding relevant passages" lines={2} />
        )}
      </div>
    </article>
  )
}

function AssistantMessage({
  message,
  canRegenerate,
  onRegenerate,
  activeCitation,
  onCitation,
  onUpdated,
}: {
  message: ChatMessage
  canRegenerate: boolean
  onRegenerate: () => void
  activeCitation: number | null
  onCitation: (n: number) => void
  onUpdated: (message: ChatMessage) => void
}) {
  const toast = useToast()
  const voice = useVoice()
  const { base } = useWorkspace()
  const numbers = useMemo(() => new Set(message.citations.map((c) => c.number)), [message.citations])
  const cards = useRef<HTMLDivElement>(null)

  const focusCitation = (n: number) => {
    onCitation(n)
    const node = cards.current?.querySelector<HTMLElement>(`[data-cite="${n}"]`)
    node?.scrollIntoView({ block: 'nearest' })
    node?.querySelector<HTMLElement>('a, [tabindex]')?.focus({ preventScroll: true })
  }

  return (
    <article className="msg" aria-label="Answer">
      <div className="msg__role">
        <span className="msg__role-icon" aria-hidden="true">
          <Icon name="sparkles" size={13} />
        </span>
        DocuMind
        {message.answer_status === 'insufficient_context' && <Badge tone="warning">Not found in your documents</Badge>}
        {message.status === 'cancelled' && <Badge>Stopped</Badge>}
        {message.status === 'failed' && <Badge tone="danger">Failed</Badge>}
      </div>
      <div className="msg__answer">
        {message.content ? (
          <AnswerMarkdown
            text={message.content}
            citationNumbers={message.status === 'completed' ? numbers : new Set()}
            activeCitation={activeCitation}
            onCitation={focusCitation}
          />
        ) : (
          <p className="msg__notice">
            <Icon name="info" size={15} />
            {message.status === 'failed' ? 'No answer was produced.' : 'Stopped before any text was written.'}
          </p>
        )}
        {message.status === 'failed' && (
          <p className="msg__notice">
            <Icon name="alert" size={15} /> Something went wrong while answering ({message.error_code ?? 'error'}).
          </p>
        )}
        {message.citations.length > 0 && (
          <div className="citations" ref={cards} aria-label="Sources">
            {message.citations.map((c) => (
              <CitationCard key={c.number} citation={c} active={activeCitation === c.number} />
            ))}
          </div>
        )}
        {message.warnings.map((w, i) => {
          const warning = w as { code?: unknown; message?: unknown }
          const reindex = warning.code === 'documents_need_reindex'
          return (
            <p key={i} className={reindex ? 'msg__notice msg__notice--warning' : 'msg__notice'}>
              <Icon name={reindex ? 'alert' : 'info'} size={15} />
              <span>
                {String(warning.message ?? '')}
                {reindex && (
                  <>
                    {' '}
                    <Link to={`${base}/documents#reindex`}>Re-index documents</Link>
                  </>
                )}
              </span>
            </p>
          )
        })}
        <div className="msg__actions">
          {message.content && (
            <Button
              size="sm"
              variant="ghost"
              icon="copy"
              onClick={() =>
                void navigator.clipboard
                  .writeText(message.content)
                  .then(() => toast.success('Answer copied.'))
                  .catch(() => toast.error("Couldn't copy to the clipboard."))
              }
            >
              Copy
            </Button>
          )}
          {message.content && voice.ttsEngine && (
            <Button
              size="sm"
              variant="ghost"
              icon="volume"
              onClick={() =>
                voice.speak(
                  answerToSpeech(message.content, message.citations, { speakCitations: voice.prefs.speakCitations }),
                  { label: 'answer' },
                )
              }
            >
              Listen
            </Button>
          )}
          {canRegenerate && (
            <Button size="sm" variant="ghost" icon="refresh" onClick={onRegenerate}>
              Regenerate
            </Button>
          )}
          {message.status === 'completed' && (
            <span className="msg__feedback">
              <AnswerFeedback message={message} onChange={onUpdated} />
            </span>
          )}
        </div>
      </div>
    </article>
  )
}

function SourceSummary({
  detail,
  sources,
  onEdit,
}: {
  detail?: ConversationDetail
  sources: Sources
  onEdit: () => void
}) {
  const { workspace } = useWorkspace()
  const { data: collections } = useCollections()
  // A new (unsaved) conversation only knows ids; look up the titles.
  const pending = detail ? [] : sources.documentIds.slice(0, 5)
  const titles = useQuery({
    queryKey: ['ws', workspace.id, 'documents', 'titles', pending],
    queryFn: async () => (await Promise.all(pending.map((id) => getDocument(workspace.id, id)))).map((d) => d.title),
    enabled: pending.length > 0,
  })
  const count = sources.documentIds.length + sources.collectionIds.length
  let label = 'All documents'
  if (count > 0) {
    const names = [
      ...(detail?.documents.map((d) => d.title) ?? titles.data ?? []),
      ...((detail?.collections ?? collections?.filter((c) => sources.collectionIds.includes(c.id)) ?? []).map((c) => c.name)),
    ]
    label = names.length === 1 ? names[0] ?? '1 source' : `${count} sources`
  }
  return (
    <Button size="sm" icon="layers" onClick={onEdit} aria-label={`Sources: ${label}. Change sources`}>
      <span className="truncate" style={{ maxWidth: 220 }}>
        {label}
      </span>
    </Button>
  )
}

function SourcePicker({
  initial,
  onClose,
  onSave,
}: {
  initial: Sources
  onClose: () => void
  onSave: (sources: Sources) => void
}) {
  const { workspace } = useWorkspace()
  const [q, setQ] = useState('')
  const [documentIds, setDocumentIds] = useState(new Set(initial.documentIds))
  const [collectionIds, setCollectionIds] = useState(new Set(initial.collectionIds))
  const { data: collections } = useCollections()
  const filters = { q, status: 'ready' as const, limit: 50 }
  const docs = useQuery({
    queryKey: documentKeys.list(workspace.id, filters),
    queryFn: () => listDocuments(workspace.id, filters),
    placeholderData: keepPreviousData,
  })
  const toggle = (set: Set<string>, id: string, on: boolean) => {
    const next = new Set(set)
    if (on) next.add(id)
    else next.delete(id)
    return next
  }
  const total = documentIds.size + collectionIds.size

  return (
    <Dialog
      open
      size="lg"
      title="Choose sources"
      description="Answers use the selected documents plus everything in the selected collections. Select nothing to use all documents."
      onClose={onClose}
      footer={
        <>
          <span className="muted" style={{ marginRight: 'auto' }}>
            {total === 0 ? 'All documents' : `${total} selected`}
          </span>
          {total > 0 && (
            <Button
              onClick={() => {
                setDocumentIds(new Set())
                setCollectionIds(new Set())
              }}
            >
              Clear
            </Button>
          )}
          <Button variant="primary" onClick={() => onSave({ documentIds: [...documentIds], collectionIds: [...collectionIds] })}>
            Use these sources
          </Button>
        </>
      }
    >
      <div className="stack">
        {collections && collections.length > 0 && (
          <section className="stack stack--sm" aria-labelledby="picker-collections">
            <h3 id="picker-collections">Collections</h3>
            <ul className="picker-list">
              {collections.map((c: Collection) => (
                <li key={c.id}>
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={collectionIds.has(c.id)}
                      onChange={(e) => setCollectionIds(toggle(collectionIds, c.id, e.target.checked))}
                    />
                    <span>
                      {c.name} <span className="muted">· {c.document_count} documents</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </section>
        )}
        <section className="stack stack--sm" aria-labelledby="picker-documents">
          <h3 id="picker-documents">Documents</h3>
          <Field label="Filter documents" hideLabel>
            <TextInput type="search" placeholder="Filter ready documents…" value={q} onChange={(e) => setQ(e.target.value)} />
          </Field>
          {docs.isLoading ? (
            <Loading lines={3} />
          ) : docs.data?.items.length ? (
            <ul className="picker-list">
              {docs.data.items.map((d: DocumentItem) => (
                <li key={d.id}>
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={documentIds.has(d.id)}
                      onChange={(e) => setDocumentIds(toggle(documentIds, d.id, e.target.checked))}
                    />
                    <span className="truncate">{d.title}</span>
                  </label>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">No ready documents{q ? ' match' : ''}.</p>
          )}
        </section>
      </div>
    </Dialog>
  )
}

function RenameConversationDialog({
  conversation,
  onClose,
  onSaved,
}: {
  conversation: { id: string; title: string }
  onClose: () => void
  onSaved: (updated: ConversationDetail) => void
}) {
  const { workspace } = useWorkspace()
  const toast = useToast()
  const [title, setTitle] = useState(conversation.title)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    try {
      const updated = await renameConversation(workspace.id, conversation.id, title.trim())
      onSaved(updated)
      toast.success('Conversation renamed.')
      onClose()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog open title="Rename conversation" onClose={onClose} size="sm">
      <form className="form" onSubmit={submit}>
        {error && <Alert>{error}</Alert>}
        <Field label="Title">
          <TextInput value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} autoFocus />
        </Field>
        <div className="form__actions">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" loading={busy} disabled={!title.trim()}>
            Save
          </Button>
        </div>
      </form>
    </Dialog>
  )
}

// ----------------------------------------------------------- history list

export function ConversationsPage() {
  useTitle('Conversations')
  const { workspace, base } = useWorkspace()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()
  const confirm = useConfirm()
  const [params, setParams] = useSearchParams()
  const q = params.get('q') ?? ''
  const offset = Number(params.get('offset') ?? 0) || 0
  const [search, setSearch] = useState(q)
  const [renaming, setRenaming] = useState<{ id: string; title: string } | null>(null)
  const limit = 20
  const query = useQuery({
    queryKey: conversationKeys.list(workspace.id, q, offset, limit),
    queryFn: () => listConversations(workspace.id, { q, offset, limit }),
    placeholderData: keepPreviousData,
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['ws', workspace.id, 'conversations', 'list'] })
  const remove = async (id: string, title: string) => {
    const ok = await confirm({
      title: `Delete "${title}"?`,
      description: 'Its messages and citations are deleted. Your documents are not affected.',
      confirmLabel: 'Delete',
      danger: true,
    })
    if (!ok) return
    try {
      await deleteConversation(workspace.id, id)
      await refresh()
      toast.success('Conversation deleted.')
    } catch (err) {
      toast.error(errorMessage(err))
    }
  }

  return (
    <>
      <PageHeader
        title="Conversations"
        description="Your conversations in this workspace. They are private to you."
        actions={
          <Button variant="primary" icon="plus" onClick={() => navigate(`${base}/chat`, { state: { fresh: Date.now() } })}>
            New conversation
          </Button>
        }
      />
      <form
        className="toolbar"
        role="search"
        aria-label="Search conversations"
        onSubmit={(e) => {
          e.preventDefault()
          setParams(search.trim() ? { q: search.trim() } : {}, { replace: true })
        }}
      >
        <div className="toolbar__grow">
          <Field label="Search titles" hideLabel>
            <TextInput type="search" placeholder="Search conversations…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </Field>
        </div>
        <Button type="submit" icon="search">
          Search
        </Button>
      </form>
      {query.isLoading ? (
        <Loading label="Loading conversations" lines={5} />
      ) : query.error ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : !query.data?.items.length ? (
        <EmptyState
          icon="message"
          title={q ? 'No conversations match' : 'No conversations yet'}
          description={q ? 'Try a different search.' : 'Ask a question about your documents to start one.'}
          action={!q && <Link className="btn btn--primary btn--md" to={`${base}/chat`}>Ask a question</Link>}
        />
      ) : (
        <>
          <div className="panel">
            <ul className="list">
              {query.data.items.map((c) => (
                <li key={c.id} className="list-row">
                  <span className="file-icon" aria-hidden="true">
                    <Icon name="message" size={16} />
                  </span>
                  <div className="list-row__main">
                    <Link to={`${base}/chat/${c.id}`} className="list-row__title truncate">
                      {c.title}
                    </Link>
                    <span className="list-row__meta">
                      {Math.ceil(c.message_count / 2)} {c.message_count <= 2 ? 'question' : 'questions'} ·{' '}
                      {c.scope === 'all' ? 'All documents' : `${c.document_ids.length + c.collection_ids.length} sources`} ·{' '}
                      {formatRelative(c.last_message_at)}
                    </span>
                  </div>
                  <Menu
                    label={`Actions for ${c.title}`}
                    trigger={<Icon name="more" />}
                    triggerClassName="btn btn--ghost btn--sm btn--icon"
                    items={[
                      { label: 'Open', icon: 'eye', onSelect: () => navigate(`${base}/chat/${c.id}`) },
                      { label: 'Rename', icon: 'edit', onSelect: () => setRenaming({ id: c.id, title: c.title }) },
                      'separator',
                      { label: 'Delete', icon: 'trash', danger: true, onSelect: () => void remove(c.id, c.title) },
                    ]}
                  />
                </li>
              ))}
            </ul>
          </div>
          <Pagination
            offset={offset}
            limit={limit}
            total={query.data.total}
            onChange={(next) => setParams({ ...(q ? { q } : {}), offset: String(next) }, { replace: true })}
            label="conversations"
          />
        </>
      )}
      {renaming && (
        <RenameConversationDialog conversation={renaming} onClose={() => setRenaming(null)} onSaved={() => void refresh()} />
      )}
    </>
  )
}


/** Push-to-talk: press to start listening, press again (or pause speaking) to finish. */
function MicButton({ onText }: { onText: (text: string) => void }) {
  const voice = useVoice()
  const { workspace } = useWorkspace()
  const toast = useToast()
  const [listening, setListening] = useState(false)
  const [busy, setBusy] = useState(false)
  const [interim, setInterim] = useState('')
  const [status, setStatus] = useState('')
  const session = useRef<Dictation | null>(null)

  const finish = useCallback(
    async (promise: Promise<string>) => {
      setBusy(true)
      try {
        const text = (await promise).trim()
        if (text) {
          setStatus(`Heard: ${text}`)
          onText(text)
        } else {
          setStatus("I didn't catch that.")
        }
      } catch (err) {
        toast.error(friendlyDictationError(err))
        setStatus('')
      } finally {
        session.current = null
        setListening(false)
        setBusy(false)
        setInterim('')
      }
    },
    [onText, toast],
  )

  const toggle = useCallback(async () => {
    if (busy) return
    if (session.current) {
      void finish(session.current.stop())
      return
    }
    const engine = voice.sttEngine
    if (!engine) return
    voice.stop() // never record our own voice
    try {
      const dictation = await startDictation({
        engine,
        workspaceId: workspace.id,
        language: voice.prefs.language || undefined,
        onInterim: setInterim,
      })
      session.current = dictation
      setListening(true)
      setStatus('Listening. Press the microphone button or Control Shift Space when you are done.')
      void dictation.done.then(() => {
        if (session.current === dictation) void finish(dictation.done)
      }, () => {
        if (session.current === dictation) void finish(dictation.done)
      })
    } catch (err) {
      toast.error(friendlyDictationError(err))
    }
  }, [busy, finish, toast, voice, workspace.id])

  useEffect(() => {
    voice.setDictationShortcut(() => void toggle())
    return () => voice.setDictationShortcut(null)
  }, [toggle, voice])

  useEffect(() => () => session.current?.cancel(), [])

  return (
    <>
      {listening && (
        <span className="listening" aria-hidden="true">
          <span className="listening__dot" />
          {interim || 'Listening…'}
        </span>
      )}
      <Button
        className="mic-button"
        variant="ghost"
        icon="mic"
        label={listening ? 'Stop voice input' : 'Ask by voice'}
        aria-pressed={listening}
        aria-keyshortcuts="Control+Shift+Space"
        loading={busy}
        onClick={() => void toggle()}
      />
      <span className="sr-only" role="status" aria-live="polite">
        {status}
      </span>
    </>
  )
}
