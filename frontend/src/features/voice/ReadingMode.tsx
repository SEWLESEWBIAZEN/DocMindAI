import { useMutation } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'

import { Alert, Button, Field, Select, Spinner } from '../../components/ui'
import { apiClient } from '../../lib/api/client'
import { errorMessage } from '../../lib/api/errors'
import type { ExtractedPage, Schemas } from '../../lib/api/types'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { AnswerMarkdown } from '../chat/AnswerMarkdown'
import { CitationCard } from '../chat/CitationCard'
import { answerToSpeech, nextSectionIndex, pageName, pageToSpeech } from './text'
import { useVoice } from './VoiceContext'

export interface ReadingPosition {
  pageIndex: number
  sentence: string
}

type Summary = Schemas['AnswerResponse']

/**
 * Listening mode for a document (ADR 0011): read a page, move by page or
 * section, summarize what you're on, and ask about it. Reading continues
 * onto the next page until stopped.
 */
export function ReadingControls({
  title,
  documentId,
  pages,
  position,
  onPosition,
  onClose,
}: {
  title: string
  documentId: string
  pages: ExtractedPage[]
  position: ReadingPosition | null
  onPosition: (position: ReadingPosition | null) => void
  onClose: () => void
}) {
  const voice = useVoice()
  const { workspace } = useWorkspace()
  const navigate = useNavigate()
  const [selected, setSelected] = useState(0)
  const index = position?.pageIndex ?? selected
  const page = pages[index]

  const read = (pageIndex: number) => {
    const target = pages[pageIndex]
    if (!target) return
    setSelected(pageIndex)
    const started = voice.speak(pageToSpeech(target), {
      label: `${title}, ${pageName(target)}`,
      onSentence: (sentence) => onPosition({ pageIndex, sentence }),
      onEnd: () => {
        if (pageIndex + 1 < pages.length) read(pageIndex + 1)
        else onPosition(null)
      },
    })
    if (!started) onPosition(null)
  }

  // Leaving reading mode stops the voice.
  const stopVoice = voice.stop
  useEffect(() => () => stopVoice(), [stopVoice])

  const summary = useMutation({
    mutationFn: async (): Promise<Summary> => {
      const number = page?.page_number
      const body = number ? { page_start: number, page_end: number } : {}
      return (await apiClient.post<Summary>(`/workspaces/${workspace.id}/documents/${documentId}/summary`, body)).data
    },
    onSuccess: (result) => {
      voice.speak(
        [`Summary of ${page ? pageName(page) : 'the document'}.`, ...answerToSpeech(result.answer, result.citations, { speakCitations: voice.prefs.speakCitations })],
        { label: `summary of ${page ? pageName(page) : title}` },
      )
    },
  })

  const sectionTarget = useMemo(() => nextSectionIndex(pages, index), [pages, index])
  const reading = voice.state.status !== 'idle' && position !== null

  return (
    <section className="stack stack--sm" aria-label="Listening mode">
      <div className="reading-panel">
        <span className="reading-panel__label">
          {reading ? <>Reading {page ? pageName(page) : ''}</> : 'Listen to this document'}
        </span>
        {pages.length > 1 && (
          <Field label="Start at" hideLabel>
            <Select
              value={index}
              onChange={(e) => {
                const next = Number(e.target.value)
                setSelected(next)
                if (reading) read(next)
              }}
              aria-label="Page to read"
              style={{ width: 130 }}
            >
              {pages.map((p, i) => (
                <option key={p.position} value={i}>
                  {p.page_number !== null ? `Page ${p.page_number}` : 'Text'}
                </option>
              ))}
            </Select>
          </Field>
        )}
        <Button size="sm" icon="arrowLeft" disabled={index <= 0} onClick={() => read(index - 1)}>
          Previous page
        </Button>
        <Button size="sm" variant="primary" icon={reading ? 'refresh' : 'play'} onClick={() => read(index)}>
          {reading ? 'Restart page' : 'Read'}
        </Button>
        <Button size="sm" icon="arrowRight" disabled={index >= pages.length - 1} onClick={() => read(index + 1)}>
          Next page
        </Button>
        <Button size="sm" icon="chevronRight" disabled={sectionTarget === null} onClick={() => sectionTarget !== null && read(sectionTarget)}>
          Next section
        </Button>
        <Button size="sm" icon="sparkles" loading={summary.isPending} onClick={() => summary.mutate()}>
          Summarize {page && page.page_number !== null ? 'this page' : 'document'}
        </Button>
        <Button size="sm" icon="message" onClick={() => navigate(`/w/${workspace.id}/chat?documents=${documentId}`)}>
          Ask about it
        </Button>
        <Button size="sm" variant="ghost" icon="x" label="Close listening mode" onClick={onClose} />
      </div>
      {summary.isPending && (
        <p className="row muted" role="status">
          <Spinner size={14} /> Summarizing…
        </p>
      )}
      {summary.error && <Alert>{errorMessage(summary.error)}</Alert>}
      {summary.data && (
        <section className="panel panel__body stack stack--sm" aria-label="Summary">
          <h2>Summary of {page ? pageName(page) : 'the document'}</h2>
          <AnswerMarkdown text={summary.data.answer} citationNumbers={new Set(summary.data.citations.map((c) => c.number))} />
          {summary.data.citations.length > 0 && (
            <div className="citations">
              {summary.data.citations.map((c) => (
                <CitationCard key={c.number} citation={{ ...c, document_available: true }} active={false} />
              ))}
            </div>
          )}
        </section>
      )}
    </section>
  )
}
