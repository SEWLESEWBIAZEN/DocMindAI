import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router'

import { Button, Icon, Spinner } from '../../components/ui'
import { apiClient } from '../../lib/api/client'
import { errorMessage } from '../../lib/api/errors'
import type { Schemas } from '../../lib/api/types'
import { AnswerMarkdown } from '../chat/AnswerMarkdown'
import { STARTERS, citationPlace } from './intelligence'

type Answer = Schemas['AnswerResponse']
type Citation = Schemas['CitationResponse']

interface Turn {
  id: number
  question: string
  answer?: Answer
  error?: string
}

export type PanelTab = 'insights' | 'metadata'

async function askDocument(ws: string, documentId: string, question: string): Promise<Answer> {
  return (await apiClient.post<Answer>(`/workspaces/${ws}/ask`, { question, document_ids: [documentId] })).data
}

async function summarize(ws: string, documentId: string): Promise<Answer> {
  return (await apiClient.post<Answer>(`/workspaces/${ws}/documents/${documentId}/summary`, {})).data
}

function Cited({ answer, onCite }: { answer: Answer; onCite: (c: Citation) => void }) {
  return (
    <>
      <AnswerMarkdown
        text={answer.answer}
        citationNumbers={new Set(answer.citations.map((c) => c.number))}
        onCitation={(n) => {
          const c = answer.citations.find((x) => x.number === n)
          if (c) onCite(c)
        }}
      />
      {answer.citations.length > 0 && (
        <ul className="intel__cites" aria-label="Sources">
          {answer.citations.map((c) => (
            <li key={c.number}>
              <button type="button" className="intel__cite" aria-label={`Show source ${c.number}: ${c.spoken_label}`} onClick={() => onCite(c)}>
                <span className="intel__cite-num" aria-hidden="true">
                  {c.number}
                </span>
                {citationPlace(c)}
              </button>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

/**
 * The panel beside the document: "AI insights" (an on-demand summary and questions
 * answered from this document only) and "Metadata" (passed in as children). Citations
 * scroll the viewer to the cited passage and highlight it. Answers aren't saved; "Open in
 * chat" starts a saved conversation about the same document.
 */
export function DocumentIntelligence({
  workspaceId,
  documentId,
  chatHref,
  canAsk,
  tab: requested,
  onTab,
  focusSignal,
  onCite,
  onClose,
  children,
}: {
  workspaceId: string
  documentId: string
  chatHref: string
  canAsk: boolean
  tab: PanelTab
  onTab: (tab: PanelTab) => void
  /** Changes whenever the page's "Ask about this document" button is pressed. */
  focusSignal: number
  onCite: (page: number | null, excerpt: string) => void
  onClose: () => void
  children: ReactNode
}) {
  const tab: PanelTab = canAsk ? requested : 'metadata'
  const setTab = onTab
  const [turns, setTurns] = useState<Turn[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [summary, setSummary] = useState<{ loading: boolean; answer?: Answer; error?: string }>({ loading: false })
  const nextId = useRef(1)
  const log = useRef<HTMLDivElement>(null)
  const box = useRef<HTMLTextAreaElement>(null)
  const cite = (c: Citation) => onCite(c.page_start, c.excerpt)

  useEffect(() => {
    if (!focusSignal || !canAsk) return
    requestAnimationFrame(() => box.current?.focus())
  }, [focusSignal, canAsk])

  useEffect(() => {
    log.current?.lastElementChild?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [turns])

  const ask = async (question: string) => {
    const q = question.trim()
    if (!q || busy) return
    const id = nextId.current++
    setDraft('')
    setBusy(true)
    setTurns((t) => [...t, { id, question: q }])
    try {
      const answer = await askDocument(workspaceId, documentId, q)
      setTurns((t) => t.map((turn) => (turn.id === id ? { ...turn, answer } : turn)))
    } catch (err) {
      setTurns((t) => t.map((turn) => (turn.id === id ? { ...turn, error: errorMessage(err) } : turn)))
    } finally {
      setBusy(false)
    }
  }

  const makeSummary = async () => {
    setSummary({ loading: true })
    try {
      setSummary({ loading: false, answer: await summarize(workspaceId, documentId) })
    } catch (err) {
      setSummary({ loading: false, error: errorMessage(err) })
    }
  }

  const tabs: { id: PanelTab; label: string }[] = [
    ...(canAsk ? [{ id: 'insights' as const, label: 'AI insights' }] : []),
    { id: 'metadata', label: 'Metadata' },
  ]

  return (
    <section className="intel" aria-label="Document intelligence">
      <header className="intel__header">
        <div className="tabs" role="tablist" aria-label="Document panel">
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              id={`intel-tab-${t.id}`}
              aria-selected={tab === t.id}
              aria-controls={`intel-panel-${t.id}`}
              className="tabs__tab"
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <Button variant="ghost" size="sm" icon="x" label="Hide document panel" onClick={onClose} />
      </header>

      {tab === 'metadata' ? (
        <div className="intel__body" role="tabpanel" id="intel-panel-metadata" aria-labelledby="intel-tab-metadata">
          {children}
        </div>
      ) : (
        <>
          <div className="intel__body" role="tabpanel" id="intel-panel-insights" aria-labelledby="intel-tab-insights" ref={log} aria-live="polite">
            <section className="intel__card" aria-labelledby="intel-summary">
              <div className="intel__card-head">
                <h3 id="intel-summary">Document summary</h3>
                {!summary.answer && (
                  <Button size="sm" icon="sparkles" loading={summary.loading} onClick={() => void makeSummary()}>
                    Summarize
                  </Button>
                )}
              </div>
              {summary.error && (
                <p className="intel__error" role="alert">
                  {summary.error}
                </p>
              )}
              {summary.answer ? (
                <div className="intel__answer">
                  <Cited answer={summary.answer} onCite={cite} />
                </div>
              ) : (
                !summary.loading && <p className="muted intel__hint">A short overview, with a source for every point.</p>
              )}
            </section>

            {turns.length === 0 ? (
              <section className="intel__card" aria-label="Suggested questions">
                <h3>Ask this document</h3>
                <ul className="intel__starters">
                  {STARTERS.map((s) => (
                    <li key={s}>
                      <button type="button" className="intel__starter" onClick={() => void ask(s)} disabled={busy}>
                        {s}
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            ) : (
              turns.map((turn) => (
                <div key={turn.id} className="intel__turn">
                  <p className="intel__question">{turn.question}</p>
                  {turn.error ? (
                    <p className="intel__error" role="alert">
                      {turn.error}
                    </p>
                  ) : !turn.answer ? (
                    <p className="intel__pending" role="status">
                      <Spinner /> Reading the document…
                    </p>
                  ) : (
                    <article className="intel__answer" aria-label="Answer">
                      {turn.answer.status === 'insufficient_context' && (
                        <p className="intel__notice">This document doesn't seem to answer that.</p>
                      )}
                      <Cited answer={turn.answer} onCite={cite} />
                    </article>
                  )}
                </div>
              ))
            )}
          </div>
          <form
            className="intel__composer"
            onSubmit={(e) => {
              e.preventDefault()
              void ask(draft)
            }}
          >
            <label htmlFor="intel-question" className="sr-only">
              Ask about this document
            </label>
            <textarea
              id="intel-question"
              ref={box}
              rows={2}
              value={draft}
              placeholder="Ask about this document…"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  void ask(draft)
                }
              }}
            />
            <Button type="submit" variant="primary" icon="send" label="Send question" disabled={busy || !draft.trim()} />
          </form>
          <p className="intel__foot">
            <Icon name="message" size={13} /> <Link to={chatHref}>Open in chat</Link> to keep this conversation.
          </p>
        </>
      )}
    </section>
  )
}
