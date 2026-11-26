import { useState, type FormEvent } from 'react'

import { Button, Dialog, Field, TextArea, useToast } from '../../components/ui'
import { errorMessage } from '../../lib/api/errors'
import type { ChatMessage } from '../../lib/api/types'
import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import { clearRating, rateAnswer, type FeedbackReason } from './api'
import { REASON_LABELS } from './feedbackReasons'


/** Thumbs up / down under a finished answer (ADR 0038). */
export function AnswerFeedback({
  message,
  onChange,
}: {
  message: ChatMessage
  onChange: (updated: ChatMessage) => void
}) {
  const { workspace } = useWorkspace()
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(false)
  const rating = message.feedback?.rating ?? null

  const run = async (work: () => Promise<ChatMessage>, done: string) => {
    setBusy(true)
    try {
      onChange(await work())
      toast.success(done)
    } catch (error) {
      toast.error(errorMessage(error))
    } finally {
      setBusy(false)
    }
  }

  const undo = () =>
    run(async () => {
      await clearRating(workspace.id, message.conversation_id, message.id)
      return { ...message, feedback: null }
    }, 'Rating removed.')

  const up = () =>
    rating === 'up'
      ? undo()
      : run(() => rateAnswer(workspace.id, message.conversation_id, message.id, { rating: 'up', shared: false }), 'Thanks for the feedback.')

  return (
    <>
      <Button
        size="sm"
        variant="ghost"
        icon="thumbsUp"
        label="Good answer"
        aria-pressed={rating === 'up'}
        className={rating === 'up' ? 'feedback-btn feedback-btn--on' : 'feedback-btn'}
        disabled={busy}
        onClick={() => void up()}
      />
      <Button
        size="sm"
        variant="ghost"
        icon="thumbsDown"
        label="Bad answer"
        aria-pressed={rating === 'down'}
        className={rating === 'down' ? 'feedback-btn feedback-btn--on' : 'feedback-btn'}
        disabled={busy}
        onClick={() => (rating === 'down' ? void undo() : setOpen(true))}
      />
      {open && (
        <DownDialog
          shareable={workspace.kind === 'organization'}
          onClose={() => setOpen(false)}
          onSubmit={(body) =>
            run(async () => {
              const updated = await rateAnswer(workspace.id, message.conversation_id, message.id, { rating: 'down', ...body })
              setOpen(false)
              return updated
            }, 'Thanks — this helps improve answers.')
          }
        />
      )}
    </>
  )
}

function DownDialog({
  shareable,
  onClose,
  onSubmit,
}: {
  shareable: boolean
  onClose: () => void
  onSubmit: (body: { reasons: FeedbackReason[]; comment: string | null; shared: boolean }) => Promise<void>
}) {
  const [reasons, setReasons] = useState<FeedbackReason[]>([])
  const [comment, setComment] = useState('')
  const [shared, setShared] = useState(false)
  const [busy, setBusy] = useState(false)
  const toggle = (r: FeedbackReason) =>
    setReasons((current) => (current.includes(r) ? current.filter((x) => x !== r) : [...current, r]))

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    await onSubmit({ reasons, comment: comment.trim() || null, shared })
    setBusy(false)
  }

  return (
    <Dialog
      open
      title="What was wrong with this answer?"
      description="Optional, but it helps us find what to fix."
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" form="answer-feedback" loading={busy}>
            Send feedback
          </Button>
        </>
      }
    >
      <form id="answer-feedback" className="stack" onSubmit={(e) => void submit(e)}>
        <fieldset className="fieldset">
          <legend className="field__label">Reasons</legend>
          <div className="stack stack--sm">
            {(Object.keys(REASON_LABELS) as FeedbackReason[]).map((r) => (
              <label key={r} className="checkbox">
                <input type="checkbox" checked={reasons.includes(r)} onChange={() => toggle(r)} />
                {REASON_LABELS[r]}
              </label>
            ))}
          </div>
        </fieldset>
        <Field label="Anything else? (optional)">
          <TextArea value={comment} maxLength={1000} rows={3} onChange={(e) => setComment(e.target.value)} />
        </Field>
        {shareable && (
          <label className="checkbox">
            <input type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} />
            <span>
              Share this question and answer with workspace admins
              <span className="field__hint" style={{ display: 'block' }}>
                Your conversations are private. Without this, admins only see that an answer was rated, never what was asked.
              </span>
            </span>
          </label>
        )}
      </form>
    </Dialog>
  )
}
